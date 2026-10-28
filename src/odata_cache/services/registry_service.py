"""Реестр сущностей: правила включения и разрешения доступа (шаг 4).

Ключевые бизнес-правила (по ТЗ):

1. **Ничего не грузим «всё подряд».** Разбор $metadata только пополняет реестр
   записями со ``state='discovered'`` — таблицы данных НЕ создаются, в 1С мы
   не ходим.
2. Сущность начинает кешироваться одним из двух путей:
   - админ явно добавил её в расписание (``SyncJob.enabled=True``) → state
     ``scheduled``;
   - первый запрос клиента к ещё не закэшированной сущности → state
     ``on_demand`` (загрузка выполняется worker’ом, шаг 8/9).
3. **Запрещено → нет ни данных, ни ссылок.** Если сущность выключена
   (``is_active=False`` или ``cacheable=False``), она не кешируется, ссылки на
   неё (``ref_entity_id``) игнорируются: колонка-FK не создаётся, поле не
   попадает в вывод API даже через expand.
4. Доступ = роль ↔ сущность: глобальный переключатель (``is_active``) +
   по-ролевые grants (``role_entities``). Разрешение считает
   :func:`allowed_entities`.

Слой не знает про HTTP и про 1С — только ORM + чистые функции.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from odata_cache.metadata import EntityTypeInfo, MetadataModel
from odata_cache.models.registry import (
    Entity,
    EntityProperty,
    staging_table_for,
    storage_table_for,
)

logger = logging.getLogger(__name__)

# Префиксы имён EntitySet в 1С (области метаданных). Нужны для эвристики целей
# ссылок: свойство ``Склад_Key`` → EntitySet ``Catalog_Склады`` и т.п.
_SET_PREFIXES = (
    "Catalog_",
    "Document_",
    "ChartOfAccounts_",
    "ChartOfCharacteristicTypes_",
    "ChartOfCalculationTypes_",
    "InformationRegister_",
    "AccumulationRegister_",
    "CalculationRegister_",
    "BusinessProcess_",
    "Task_",
    "Enum_",
)

# --- состояния жизненного цикла кеширования ---
STATE_DISCOVERED = "discovered"   # найдена в $metadata, ничего не грузим
STATE_SCHEDULED = "scheduled"     # добавлена админом в расписание
STATE_ON_DEMAND = "on_demand"     # закэширована по первому запросу клиента
STATE_DISABLED = "disabled"       # явно выключена → данные удалены, ссылки убраны

STATES = (STATE_DISCOVERED, STATE_SCHEDULED, STATE_ON_DEMAND, STATE_DISABLED)


@dataclass(slots=True)
class SyncResult:
    """Итог разбора $metadata в реестр (для ответа админки/логов)."""

    created: list[str]
    updated: list[str]
    unchanged: list[str]
    removed: list[str]


def _value_type_for(edm_type: str) -> str:
    """Edm.* → наш логический тип колонки (одинаково работает на PG и SQLite)."""
    t = edm_type.lower()
    return {
        "string": "string",
        "guid": "guid",
        "boolean": "bool",
        "int16": "int",
        "int32": "int",
        "int64": "int",
        "byte": "int",
        "double": "float",
        "single": "float",
        "decimal": "decimal",
        "datetime": "datetime",
        "date": "date",
        "duration": "string",
    }.get(t, "string")


# ---------------------------------------------------------------------------
# разбор $metadata → реестр
# ---------------------------------------------------------------------------

def sync_metadata_to_registry(session: Session, model: MetadataModel) -> SyncResult:
    """Разложить разобранный $metadata в ``entities`` / ``entity_properties``.

    ВАЖНО: функция НЕ создаёт таблиц данных и НЕ обращается к 1С — это чисто
    регистровый шаг («узнали схему»). Таблица появится только когда сущность
    включат в расписание или к ней впервые обратятся (см. ensure_loadable()).

    Идемпотентность: повторный разбор не меняет ``schema_version``, если набор
    свойств идентичен; при изменении схемы версия растёт (DDL пересоберём на шаге 8).
    """
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []

    known_types = set(model.entity_types)

    for set_name, type_name in sorted(model.entity_sets.items()):
        info = model.entity_types.get(type_name)
        if info is None:
            logger.warning(
                "EntitySet %s ссылается на неизвестный EntityType %s", set_name, type_name
            )
            continue

        entity = session.scalar(select(Entity).where(Entity.name == set_name))
        plan = _plan_properties(model, info, known_types)

        if entity is None:
            entity = Entity(
                name=set_name,
                odata_type_name=_qualified(model.namespace, type_name),
                entity_set_name=set_name,
                key_property=(info.keys[0] if info.keys else "Key"),
                title_property=_pick_title(info),
                is_active=False,          # ← правило 1: по умолчанию ЗАПРЕЩЕНА
                cacheable=True,
                state=STATE_DISCOVERED,
                storage_table=storage_table_for(set_name),
                staging_table=staging_table_for(set_name),
                schema_version=1,
            )
            session.add(entity)
            session.flush()
            _replace_properties(session, entity, plan)
            created.append(set_name)
            continue

        # Сравнение схемы — по нормализованным колонкам (см. _norm_columns):
        # план из $metadata и набор EntityProperty из БД приводятся к одному
        # виду, поэтому повторный разбор идемпотентен.
        known_types = set(model.entity_types)
        new_cols = _norm_columns(_plan_columns(info, known_types))
        old_cols = _norm_columns(
            [
                (
                    p.name, p.column_name, p.edm_type, p.nullable,
                    p.is_key, bool(p.ref_kind), p.ref_entity_id,
                )
                for p in entity.properties
            ],
            id_to_type=_id_to_short_type(session),
        )
        if new_cols != old_cols:
            entity.schema_version += 1
            _replace_properties(session, entity, plan)
            updated.append(set_name)
        else:
            unchanged.append(set_name)
        entity.odata_type_name = _qualified(model.namespace, type_name)
        entity.entity_set_name = set_name

    # Исчезнувшие из метаданных: снимаем с расписания, строку храним для истории
    # (данные удалит worker на шаге 8 вместе со ссылками).
    live_names = set(model.entity_sets)
    for other in session.scalars(select(Entity)):
        if other.entity_set_name and other.entity_set_name not in live_names:
            if other.state != STATE_DISABLED:
                other.state = STATE_DISABLED
                other.is_active = False
                removed.append(other.name)

    resolve_all_ref_targets(session)
    return SyncResult(created, updated, unchanged, removed)


def _qualified(namespace: str, type_name: str) -> str:
    return f"{namespace}.{type_name}" if namespace else type_name


def _plan_properties(
    model: MetadataModel, info: EntityTypeInfo, known_types: set[str]
) -> list[tuple]:
    """Свойства сущности для записи в ``entity_properties``.

    Кортеж: (name, column_name, edm_type, value_type, nullable, max_length,
             precision, scale, is_key, ref_kind, ref_target_type, position)

    Навигации и коллекции (RecordSet) колонками не становятся: коллекция — это
    отдельная дочерняя таблица, она регистрируется как самостоятельный EntitySet.
    """
    keys = set(info.keys)
    plan: list[tuple] = []
    pos = 0
    for name, prop in info.properties.items():
        if prop.is_navigation or prop.is_collection:
            continue
        ref_target = prop.ref_target if prop.ref_target in known_types else None
        plan.append(
            (
                name,
                name,                       # колонка = имя 1С как есть (кириллица ок)
                prop.type_name,
                _value_type_for(prop.type_name),
                prop.nullable,
                prop.max_length,
                prop.precision,
                prop.scale,
                name in keys,
                (prop.ref_kind or "ref") if ref_target else None,
                ref_target,
                pos,
            )
        )
        pos += 1
    return plan


def _replace_properties(session: Session, entity: Entity, plan: list[tuple]) -> None:
    """Полностью пересобрать свойства (набор небольшой — проще заменить, чем диффить).

    Сохраняем уже проставленные ``ref_entity_id`` по имени свойства: при
    повторном sync цель ссылки не должна «теряться», иначе отпечаток схемы
    запрыгает и needs/plan увидят несвязанные свойства.
    """
    old_refs = {p.name: p.ref_entity_id for p in entity.properties}
    for old in list(entity.properties):
        session.delete(old)
    session.flush()

    for row in plan:
        (
            name, column_name, edm_type, value_type, nullable, max_length,
            precision, scale, is_key, ref_kind, _ref_target, position,
        ) = row
        session.add(
            EntityProperty(
                entity_id=entity.id,
                name=name,
                column_name=column_name,
                edm_type=edm_type,
                value_type=value_type,
                nullable=nullable,
                max_length=max_length,
                precision=precision,
                scale=scale,
                is_key=is_key,
                exposed=True,
                indexed=bool(is_key or ref_kind),
                ref_kind=ref_kind,
                ref_entity_id=old_refs.get(name),
                position=position,
            )
        )
    session.flush()
    _resolve_ref_targets(session, entity, plan)


def _resolve_ref_targets(
    session: Session, entity: Entity, plan: list[tuple]
) -> None:
    """Проставить ``ref_entity_id`` (FK на строку реестра целевой сущности).

    Целевая сущность может ещё не существовать в реестре (её нет в EntitySet’ах
    1С) — тогда ссылка остаётся «висячей» без FK: в БД это будет просто Guid-колонка,
    а в API поле не раскрывается через expand. Так работает правило 3.
    """
    by_id = {e.id: e for e in session.scalars(select(Entity))}

    def _short_type_name(e: Entity) -> str:
        return (e.odata_type_name or "").rsplit(".", 1)[-1] or e.name

    def _set_ref(prop_name: str, target_id: int | None) -> None:
        """Проставить FK цели ссылки.

        Свойства пересоздаются пачкой, поэтому ищем их НЕ по объектам в памяти
        (их порядок/идентичность после cascade-флешей ненадёжна), а по данным —
        через SELECT: он принудительно flush'ит сессию и возвращает актуальные
        строки EntityProperty этой сущности.
        """
        if target_id is None:
            return
        row = session.scalar(
            select(EntityProperty).where(
                EntityProperty.entity_id == entity.id,
                EntityProperty.name == prop_name,
            )
        )
        if row is not None and row.ref_kind:
            row.ref_entity_id = target_id

    # 1) по плану (Association из $metadata): цель — короткое имя EntityType
    for row in plan:
        ref_target = row[10]
        if not ref_target:
            continue
        target = next(
            (e for e in by_id.values() if _short_type_name(e) == ref_target), None
        ) or _guess_set_by_suffix(session, ref_target)
        t = target if isinstance(target, Entity) else (
            session.scalar(select(Entity).where(Entity.name == target)) if target else None
        )
        if t is not None and t.id != entity.id:
            _set_ref(row[0], t.id)

    # 2) доводка эвристикой по имени свойства (Номенклатура_Key → Catalog_Номенклатура),
    #    когда цель не разрешилась через Association (1С иногда их не объявляет)
    type_to_ent: dict[str, Entity] = {}
    for e in by_id.values():
        type_to_ent.setdefault(_short_type_name(e), e)
        type_to_ent.setdefault(e.name, e)
    fresh_props = session.scalars(
        select(EntityProperty).where(EntityProperty.entity_id == entity.id)
    ).all()
    for ep in fresh_props:
        if ep.ref_kind and ep.ref_entity_id is None:
            hit = _find_ref_target(session, entity, ep, type_to_ent)
            if hit is not None:
                ep.ref_entity_id = hit.id
    session.flush()


def _guess_set_by_suffix(session: Session, type_name: str) -> str | None:
    """Если цель не найдена по типу — ищем сущность, чьё имя совпадает с суффиксом."""
    for prefix in ("Catalog_", "ChartOfCharacteristicTypes_", "Document_",
                   "InformationRegister_", "AccumulationRegister_"):
        if type_name.startswith(prefix):
            base = type_name[len(prefix):]
            found = session.scalar(
                select(Entity.name).where(Entity.name.in_((type_name, base))).limit(1)
            )
            if found:
                return found
    return None


# Кортеж «колонка» для сравнения схем:
# (name, column_name, edm_type, nullable, is_key, ref_kind, target_key)
# Позицию НЕ учитываем: она присваивается только колонкам-скалярам, а нумерация
# в $metadata идёт по всем свойствам (навигации/коллекции тоже занимают номер).
_COL_FIELDS = 7


def _plan_columns(info: EntityTypeInfo, known_types: set[str]) -> list[tuple]:
    """План колонок из $metadata в компактном виде для сравнения."""
    keys = set(info.keys)
    cols: list[tuple] = []
    for name, prop in info.properties.items():
        if prop.is_navigation or prop.is_collection:
            continue
        target = prop.ref_target if prop.ref_target in known_types else None
        cols.append(
            (name, name, prop.type_name, bool(prop.nullable),
             name in keys, bool(target), target or "")
        )
    return cols


def _norm_columns(rows: list[tuple], id_to_type: dict[int, str] | None = None) -> tuple:
    """Привести колонки к сопоставимому виду.

    Цель ссылки в плане из $metadata — короткое имя EntityType, а в БД —
    ``ref_entity_id``; приводим id к тому же короткому имени, поэтому повторный
    разбор идемпотентен. Незакрытая ссылка (цели нет в реестре) сравнивается как
    пустая строка с обеих сторон.
    """
    norm: list[tuple] = []
    for row in rows:
        name, column, edm, nullable, is_key, has_ref, target = row[:_COL_FIELDS]
        if isinstance(target, int):                     # ref_entity_id из ORM
            target = (id_to_type or {}).get(target, "")
        norm.append((name, column, edm, bool(nullable), bool(is_key),
                     bool(has_ref), target or ""))
    return tuple(sorted(norm))


def _id_to_short_type(session: Session) -> dict[int, str]:
    """id сущности реестра → короткое имя EntityType (для нормализации целей)."""
    out: dict[int, str] = {}
    for e in session.scalars(select(Entity)):
        short = (e.odata_type_name or "").rsplit(".", 1)[-1]
        out[e.id] = short or e.name
    return out


def resolve_all_ref_targets(session: Session) -> int:
    """Дозаполнить ``ref_entity_id`` там, где цель появилась позже.

    Первый проход по $metadata идёт по алфавиту EntitySet: ссылка на сущность,
    которая ещё не в реестре, остаётся незаполненной. Вызываем после полного
    sync_metadata_to_registry — это делает связи стабильными независимо от
    порядка обхода. Возвращаем число проставленных ссылок.
    """
    entities = list(session.scalars(select(Entity)))
    by_id = {e.id: e for e in entities}
    type_to_entity: dict[str, Entity] = {}
    for e in entities:
        short = (e.odata_type_name or "").rsplit(".", 1)[-1]
        if short:
            type_to_entity.setdefault(short, e)
        type_to_entity.setdefault(e.name, e)

    fixed = 0
    for e in entities:
        for prop in e.properties:
            if prop.ref_entity_id is not None or not prop.ref_kind:
                continue
            target = _find_ref_target(session, e, prop, type_to_entity)
            if target is not None:
                prop.ref_entity_id = target.id
                fixed += 1
    session.flush()
    del by_id
    return fixed


_REF_SUFFIXES = ("_Ref_Key", "_Key", "Key")


def ref_property_base(prop_name: str) -> str:
    """Имя свойства ссылки без ключевого суффикса: ``Склад_Key`` → ``Склад``."""
    base = prop_name
    for suffix in _REF_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def _find_ref_target(
    session: Session,
    owner: Entity,
    prop: EntityProperty,
    type_to_entity: dict[str, Entity],
) -> Entity | None:
    """Цель ссылки: сначала по имени свойства (``Номенклатура_Key`` → Номенклатура),
    затем по суффиксу имени типа.

    Имена EntitySet в 1С всегда с префиксом области (``Catalog_``, ``Document_``,
    ``AccumulationRegister_`` …), поэтому «голое» имя (``Склад``) почти никогда не
    совпадает — его тоже проверяем через перебор префиксов.
    """
    base = ref_property_base(prop.name)
    candidates = [base] + [f"{prefix}{base}" for prefix in _SET_PREFIXES]
    for cand in candidates:
        hit = type_to_entity.get(cand)
        if hit is not None and hit.id != owner.id:
            return hit
    # морфология 1С: свойство «Склад_Key» → каталог «Catalog_Склады»,
    # «ЕдиницаИзмерения_Key» → «Catalog_ЕдиницыИзмерения». Ищем по началу слова,
    # сравнение — с окончанием целевого имени (род. падеж мн. ч. в 1С).
    low = base.lower()
    best: tuple[int, Entity] | None = None
    for name, hit in type_to_entity.items():
        if hit.id == owner.id:
            continue
        other_low = _strip_set_prefix(name).lower()
        if not _prefix_match(low, other_low):
            continue
        score = len(other_low)  # берём ближайшее по длине совпадение
        if best is None or score < best[0]:
            best = (score, hit)
    if best is not None:
        return best[1]
    # последний шанс: цель спрятана в суффиксе длинного имени (…_Организация)
    for name, hit in type_to_entity.items():
        if hit.id != owner.id and (name.endswith(f"_{base}") or name == base):
            return hit
    return None


def _prefix_match(a: str, b: str) -> bool:
    """Морфологическое «похоже»: совпадает префикс без учёта чередующихся гласных.

    1С любит формы типа «ЕдиницаИзмерения» (свойство) vs «ЕдиницыИзмерения»
    (каталог): общие первые символы прерываются на «а/ы», поэтому считаем
    совпадение по согласным — их последовательность в таких парах одинакова.
    """
    if a == b:
        return True
    common = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        common += 1
    if common >= max(4, min(len(a), len(b)) - 2):
        return True
    # сравнение по согласным (беглые/чередующиеся гласные а/о/е/ы/и игнорируем)
    cons_a = [c for c in a if c not in "аеоыяиюёу"]
    cons_b = [c for c in b if c not in "аеоыяиюёу"]
    cc = 0
    for ca, cb in zip(cons_a, cons_b):
        if ca != cb:
            break
        cc += 1
    return cc >= max(5, min(len(cons_a), len(cons_b)) - 1)


def _strip_set_prefix(name: str) -> str:
    """``Catalog_Склады`` → ``Склады``; имя без известного префикса — как есть."""
    for prefix in _SET_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _pick_title(info: EntityTypeInfo) -> str | None:
    """Человекочитаемое имя записи: Description/Наименование → Name → первый String."""
    for cand in ("Description", "Наименование", "Name", "Представление"):
        if cand in info.properties:
            return cand
    for name, prop in info.properties.items():
        if prop.type_name == "String" and not prop.is_collection:
            return name
    return None
