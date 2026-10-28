"""План загрузки сущности из 1С (шаг 6).

Правило ТЗ: грузим БЕЗ $expand. Если в записи есть ссылка на сущность —
проверяем запрет: разрешена → добавляем её в план загрузки (пакетом по ключам),
запрещена → не грузим ничего, колонка ``*_Key`` остаётся простым Guid'ом и
в expand не попадает.

Модуль НЕ ходит в 1С и НЕ пишет БД: это чистый «планировщик» — на входе имя
корневой сущности + session (реестр и разрешения), на выходе упорядоченный
список задач :class:`LoadTask`. Выполнит задачи worker (шаг 8/9).

Каскад: зависимости берутся транзитивно (Номенклатура → ЕдиницаИзмерения → …),
но только по РАЗРЕШЁННЫМ сущностям (is_globally_allowed). Запрещённая ветка
обрезается целиком — вместе со ссылками на неё.

Определение целей ссылок:
1. основной источник — реестр ``entity_properties.ref_entity_id`` (его заполнил
   sync_metadata_to_registry из NavigationProperty/$metadata);
2. для свойств без FK в реестре (например служебные ``*_Type`` или поля,
   появившиеся в данных сверх OpenType) — эвристика по данным: значение вида
   ``StandardODATA.Catalog_Характеристики`` сопоставляется с EntityType цели.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from odata_cache.models.registry import Entity
from odata_cache.services.access import get_entity, is_globally_allowed
from odata_cache.services.entity_resolution import resolve_ref_target

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LoadTask:
    """Одна задача загрузки: какой EntitySet тянуть и чем фильтровать."""

    entity_name: str                # canonical-имя в реестре (= имя EntitySet в URL)
    root: bool = False              # корневая сущность запроса
    key_values: list[str] | None = None  # None ⇒ полный пакет (root/scheduled)
    #                                     или «ключи придут после загрузки родителя»
    pending_keys: bool = False      # True ⇒ ждём distinct *_Key из строк родителя
    via: str = ""                   # как узнали о задаче: schedule|on_demand|ref:<имя>
    # реальная сущность для чтения/expand, если исходная — прокладка (alias):
    reads_for: list[str] = field(default_factory=list)

    def filter_expression(self, key_property: str) -> str | None:
        """OData $filter для выборки только нужных ключей (для on-demand докачки)."""
        if not self.key_values:
            return None
        parts = ", ".join(f"'{v}'" for v in sorted(set(self.key_values)))
        return f"{key_property} in ({parts})"


@dataclass(slots=True)
class LoadPlan:
    root_entity: str
    tasks: list[LoadTask] = field(default_factory=list)
    skipped_forbidden: set[str] = field(default_factory=set)  #observability для логов

    def names(self) -> list[str]:
        return [t.entity_name for t in self.tasks]


def _type_to_entity(session: Session) -> dict[str, Entity]:
    """Имя EntityType (короткое) → сущность реестра; нужно для эвристики *_Type."""
    mapping: dict[str, Entity] = {}
    for e in session.scalars(select(Entity)):
        short = (e.odata_type_name or "").rsplit(".", 1)[-1]
        if short:
            mapping.setdefault(short, e)
        mapping.setdefault(e.name, e)
    return mapping


def build_load_plan(
    session: Session,
    root_name: str,
    *,
    mode: str = "full",
    seed_keys: dict[str, list[str]] | None = None,
    max_depth: int = 4,
) -> LoadPlan:
    """Собрать план загрузки: корень + все РАЗРЕШЁННЫЕ зависимые сущности.

    ``mode='full'`` — корень грузится целиком (scheduled/on-demand первичная
    загрузка); зависимые сущности — только теми ключами, на которые реально
    ссылаются загруженные записи (seed_keys может задать стартовый набор вручную).

    ``mode='keys'`` — корень тоже фильтруется по seed_keys (точечная докачка
    конкретных записей, например при $expand-запросе к уже живой кеш-таблице).
    """
    plan = LoadPlan(root_entity=root_name)
    root = get_entity(session, root_name)
    if root is None:
        raise ValueError(f"сущность {root_name!r} не найдена в реестре")

    visited: set[int] = {root.id}
    tasks_by_target: dict[int, LoadTask] = {}
    queue: list[tuple[Entity, list[str] | None, str, int]] = [(root, None, "", 0)]

    while queue:
        ent, keys, via, depth = queue.pop(0)
        if ent is root:
            task_keys = None if mode == "full" else keys
        else:
            task_keys = keys
        task = LoadTask(
            entity_name=ent.name,
            root=ent is root,
            key_values=task_keys,
            # зависимость без готового списка ключей: worker подставит их
            # после загрузки родителя (distinct ненулевых *_Key)
            pending_keys=(ent is not root and task_keys is None),
            via=via,
        )
        plan.tasks.append(task)
        tasks_by_target[ent.id] = task

        if depth >= max_depth:
            logger.debug("каскад обрезан по глубине: %s", ent.name)
            continue

        # цели ссылок этой сущности: из реестра (FK) + подмены (alias/link_override)
        targets: dict[int, str] = {}
        for prop in ent.properties:
            if prop.ref_kind == "collection":
                continue  # дочерние таблицы грузятся отдельным механизмом (RecordSet)
            source_id = prop.ref_entity_id
            if source_id is None:
                # нет FK в реестре: если свойство выглядит как *_Key Guid — цель неизвестна,
                # проверим по данным на шаге загрузки (_Type-эвристика в loader'е)
                continue
            resolved = resolve_ref_target(session, ent, prop, source_id)
            if resolved is None:
                continue
            target_id = resolved.effective_id
            if resolved.changed:
                # прокладка: данные лежат в target, но ссылки клиентов формально
                # указывают на source → помечаем, для каких сущностей читается target
                t = tasks_by_target.get(target_id)
                src_name = session.get(Entity, resolved.source_id)
                if t is not None and src_name is not None:
                    if src_name.name not in t.reads_for:
                        t.reads_for.append(src_name.name)
            targets[target_id] = prop.name

        for target_id, via_prop in targets.items():
            if target_id in visited:
                continue
            visited.add(target_id)
            target = session.get(Entity, target_id)
            if target is None:
                continue
            if not is_globally_allowed(target):
                # правило ТЗ: запрещено → не кешируем и ссылки в вывод не отдаём
                plan.skipped_forbidden.add(target.name)
                logger.info("ссылка %s.%s → %s: запрещено, не грузим",
                            ent.name, via_prop, target.name)
                continue
            # ключи для докачки: пока пустые — worker подставит их после
            # загрузки родителя (извлечёт distinct *_Key из выгруженных строк)
            seed = (seed_keys or {}).get(target.name)
            queue.append((target, seed, f"ref:{ent.name}.{via_prop}", depth + 1))

    return plan


def extract_ref_key_values(rows: list[dict], entity: Entity) -> dict[int, set[str]]:
    """Из выгруженных строк собрать уникальные ненулевые значения ключей ссылок.

    Результат: ``{ref_entity_id: {guid,...}}`` — по нему worker построит
    ``$filter=Ref_Key in (...)`` для зависимых сущностей. Нулевой GUID
    ('00000000-...') и пустые строки отбрасываются: это «нет ссылки» в 1С.
    ПустыеGuid трактуем как NULL — см. нормализацию в loader (шаг 8).
    """
    ZERO_GUID = "00000000-0000-0000-0000-000000000000"
    result: dict[int, set[str]] = {}
    ref_props = [p for p in entity.properties if p.ref_kind == "ref" and p.ref_entity_id]
    for row in rows:
        for prop in ref_props:
            raw = row.get(prop.name)
            if raw is None:
                continue
            val = str(raw).strip()
            if not val or val.lower() == ZERO_GUID:
                continue
            result.setdefault(prop.ref_entity_id, set()).add(val)
    return result


def merge_seed_keys(
    plan: LoadPlan, extra: dict[str, list[str]]
) -> LoadPlan:
    """Добавить ключи докачки в существующие задачи (после загрузки родителя)."""
    by_name = {t.entity_name: t for t in plan.tasks}
    for name, keys in extra.items():
        task = by_name.get(name)
        if task is None or not keys:
            continue
        merged = set(task.key_values or []) | set(keys)
        task.key_values = sorted(merged)
    return plan
