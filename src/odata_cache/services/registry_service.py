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

        # ref_target из плана нормализуем к имени сущности реестра — иначе
        # сравнение отпечатков «прыгает» между проходами разбора.
        type_to_entity_name: dict[str, str] = {}
        for e in session.scalars(select(Entity)):
            short = (e.odata_type_name or "").rsplit(".", 1)[-1]
            if short:
                type_to_entity_name.setdefault(short, e.name)
            type_to_entity_name.setdefault(e.name, e.name)
        norm_plan = [
            row[:10] + (type_to_entity_name.get(row[10], row[10]) if row[10] else None,) + row[11:]
            for row in plan
        ]
        new_fp = _fingerprint(norm_plan)
        # ref_target в плане — имя EntityType из metadata; сравниваем с целевой
        # сущностью реестра (ref_entity_id), иначе каждый проход «видел» бы
        # изменение схемы и гнал schema_version вверх.
        by_id = {e.id: e for e in session.scalars(select(Entity))}

        def _ref_of(p) -> str:
            """Нормализованная цель ссылки для отпечатка: короткое ИМЯ ТИПА цели.

            В плане цель записана как имя типа, в ORM — как ref_entity_id;
            приводим оба к одному виду (short type name), а незакрытую ссылку
            — к стабильному '~<имя свойства>', чтобы не «мерцала».
            """
            if not p.ref_kind:
                return ""
            ent = by_id.get(p.ref_entity_id) if p.ref_entity_id else None
            if ent is None:
                return f"~{p.name}"
            return (ent.odata_type_name or "").rsplit(".", 1)[-1] or ent.name

        old_fp = _fingerprint(
            [
                (
                    p.name, p.column_name, p.edm_type, p.value_type,
                    _ref_of(p),
                )
                for p in sorted(entity.properties, key=lambda x: x.position)
            ]
        )
        if new_fp != old_fp:
            entity.schema_version += 1
            _replace_properties(session, entity, norm_plan)
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
    props_by_name = {p.name: p for p in entity.properties}
    by_id = {e.id: e for e in session.scalars(select(Entity))}

    def _short_type_name(e: Entity) -> str:
        return (e.odata_type_name or "").rsplit(".", 1)[-1] or e.name

    # 1) по плану (Association из $metadata): цель — короткое имя EntityType
    for row in plan:
        ref_target = row[10]
        if not ref_target:
            continue
        ep = props_by_name.get(row[0])
        if ep is None:
            continue
        target = next(
            (e for e in by_id.values() if _short_type_name(e) == ref_target), None
        ) or _guess_set_by_suffix(session, ref_target)
        t = target if isinstance(target, Entity) else (
            session.scalar(select(Entity).where(Entity.name == target)) if target else None
        )
        if t is not None and t.id != entity.id:
            ep.ref_entity_id = t.id

    # 2) доводка эвристикой по имени свойства (Номенклатура_Key → Catalog_Номенклатура),
    #    когда цель не разрешилась через Association (1С иногда их не объявляет)
    type_to_ent: dict[str, Entity] = {}
    for e in by_id.values():
        type_to_ent.setdefault(_short_type_name(e), e)
        type_to_ent.setdefault(e.name, e)
    for ep in entity.properties:
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


def _fingerprint(rows: list[tuple]) -> tuple:
    """Отпечаток схемы сущности для сравнения «изменилось ли что-то».

    Учитываем только то, на что реагирует DDL: имя колонки, её SQL-имя, тип,
    nullability и ссылка (вид + цель). Позиция — тоже часть отпечатка: перестановка
    полей в 1С меняет порядок колонок в выгрузке.
    """
    norm: list[tuple] = []
    for row in rows:
        if len(row) >= 12:      # план из _plan_properties
            name, column, edm, vtype, nullable, _ml, _pr, _sc, is_key, ref_kind, ref_target, pos = row[:12]
            norm.append((name, column, edm, vtype, bool(nullable), bool(is_key), ref_kind or "", ref_target or "", pos))
        else:                   # кортеж из ORM-свойств (5 полей)
            name, column, edm, vtype, ref = row[:5]
            norm.append((name, column, edm, vtype, True, False, "", ref or "", len(norm)))
    return tuple(norm)


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


def _find_ref_target(
    session: Session,
    owner: Entity,
    prop: EntityProperty,
    type_to_entity: dict[str, Entity],
) -> Entity | None:
    """Цель ссылки: сначала по имени свойства (``Номенклатура_Key`` → Нomenклатура),
    затем по суффиксу имени типа."""
    base = prop.name
    for suffix in ("_Ref_Key", "_Key", "Key"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    for cand in (base, f"Catalog_{base}", f"ChartOfCharacteristicTypes_{base}"):
        hit = type_to_entity.get(cand)
        if hit is not None and hit.id != owner.id:
            return hit
    return None


def _pick_title(info: EntityTypeInfo) -> str | None:
    """Человекочитаемое имя записи: Description/Наименование → Name → первый String."""
    for cand in ("Description", "Наименование", "Name", "Представление"):
        if cand in info.properties:
            return cand
    for name, prop in info.properties.items():
        if prop.type_name == "String" and not prop.is_collection:
            return name
    return None
