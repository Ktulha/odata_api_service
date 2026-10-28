"""Оркестрация синхронизации сущности с 1С (шаг C.2).

Точка входа — :func:`sync_entity`: строит план загрузки (load_plan, без
$expand), скачивает корень и зависимые сущности пакетом через
``$filter(Key in (...))`` чанками, а дочерние коллекции (RecordSet /
ДополнительныеРеквизиты) грузит пакетным фильтром по ключам родителя с
АВТОМАТИЧЕСКИМ fallback на навигационные URL каждой записи
(``/{Set}(guid'..')/RecordSet``) — согласованный вариант «б+а».

Правила безопасности (ТЗ):
- модуль импортируется ТОЛЬКО worker'ом; data/admin API к нему не обращается;
- запрещённые сущности в план не попадают (обрезает load_plan), а значит
  никогда не запрашиваются у 1С и не пишутся в БД;
- каждая задача пишется staging → upsert (worker.loader); частичная фиксация
  допустима: упавшая задача не откатывает уже загруженные;
- прогон каждой задачи фиксируется в ``sync_runs`` — лог для админки.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Callable

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from odata_cache.models.registry import Entity, EntityProperty
from odata_cache.models.sync import SyncRun
from odata_cache.services import ddl
from odata_cache.services.access import get_entity, is_globally_allowed
from odata_cache.services.load_plan import LoadPlan, build_load_plan, merge_seed_keys
from odata_cache.worker import loader
from odata_cache.worker.odata_client import OdataClient, OdataError

logger = logging.getLogger(__name__)

# сколько ключей в один пакетный $filter ... in (...)
KEY_CHUNK = 500
# защита: nav-fallback построчно только для умеренных выборок
MAX_FALLBACK_ROWS = 20_000


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# хелперы
# ---------------------------------------------------------------------------

def _in_filter(prop_name: str, value_type: str, values: list[str]) -> str:
    """OData v3-выражение «Prop in (…)» c экранированием строковых литералов."""
    if value_type in ("int", "float", "decimal"):
        body = ", ".join(str(v) for v in values)
    else:
        body = ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)
    return f"{prop_name} in ({body})"


def _key_predicate(row: dict[str, Any], key_props: list[EntityProperty]) -> str | None:
    """Предикат ключа записи для URL: guid'..' / 'str' / число. Составной — через запятую."""
    parts: list[str] = []
    for p in key_props:
        v = row.get(p.name)
        if v is None:
            return None
        if p.value_type == "guid":
            parts.append(f"guid'{v}'")
        elif p.value_type in ("int", "float", "decimal"):
            parts.append(str(v))
        else:
            parts.append("'" + str(v).replace("'", "''") + "'")
    return ",".join(parts)


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _fetch_all(client: OdataClient, set_name: str,
               filter_expr: str | None = None) -> list[dict]:
    """Выгрузить коллекцию целиком (со страницами) в список сырых строк."""
    return list(client.iter_collection(set_name, filter_expr=filter_expr))


def _upsert_task(session: Session, engine: Engine,
                 entity: Entity, rows: list[dict], *, full: bool,
                 stats: dict) -> dict:
    """Нормализация + staging→upsert одной сущности; возвращает счётчики batch."""
    props = list(entity.properties)
    battle = ddl.ensure_table(engine, entity, props)
    staging = ddl.ensure_staging(engine, entity, props)
    key_cols = [p.column_name for p in props if p.is_key]
    columns = {p.column_name: p.value_type for p in props}
    known = {p.name for p in props}
    norm, warns = loader.normalize_rows(rows, columns, known)
    stats["warnings"].extend(warns[:3])
    return loader.load_batch(engine, staging, battle, norm, key_cols=key_cols, full=full)


def _load_child_collections(
    session: Session,
    engine: Engine,
    client: OdataClient,
    parent: Entity,
    parent_raw_rows: list[dict],
    stats: dict,
) -> None:
    """Дочерние таблицы (ref_kind='collection'): RecordSet регистров, доп. реквизиты.

    Стратегия «б+а»: сначала пакетный $filter(Parent_Key in ...) на дочерний
    EntitySet (<Parent>_RecordType в стандартном OData 1С); при отказе 1С или
    составном ключе родителя — навигационный URL каждой родительской строки.
    Запрещённая цель пропускается целиком (правило ТЗ).
    """
    coll_props = [p for p in parent.properties
                  if p.ref_kind == "collection" and p.ref_entity_id]
    if not coll_props or not parent_raw_rows:
        return

    # PK родителя берём из СЫРЫХ строк 1С (normalize_rows вырезает неизвестные поля)
    parent_key_props = [p for p in parent.properties if p.is_key]

    for prop in coll_props:
        child = session.get(Entity, prop.ref_entity_id)
        if child is None or not is_globally_allowed(child):
            continue
        child_props = list(child.properties)
        if not any(p.is_key for p in child_props):
            continue

        raw_items: list[dict] = []
        used_fallback = False

        # --- попытка 1: пакетный фильтр по первому ключу родителя -----------
        if len(parent_key_props) == 1:
            pk = parent_key_props[0]
            keys = sorted({str(r[pk.name]) for r in parent_raw_rows
                           if r.get(pk.name) is not None})
            try:
                for chunk in _chunks(keys, KEY_CHUNK):
                    expr = _in_filter(pk.name, pk.value_type, chunk)
                    raw_items.extend(_fetch_all(
                        client, child.entity_set_name or child.name, expr))
            except OdataError as e:
                logger.info("пакетная выгрузка %s не удалась (%s) → nav-fallback",
                            child.name, e)
                raw_items, used_fallback = [], True
        else:
            used_fallback = True  # составной PK — сразу построчно

        # --- попытка 2: навигационные URL ------------------------------------
        if used_fallback or not raw_items:
            if len(parent_raw_rows) > MAX_FALLBACK_ROWS:
                stats["warnings"].append(
                    f"{child.name}: nav-fallback пропущен, слишком много строк "
                    f"родителя ({len(parent_raw_rows)})"
                )
                continue
            set_name = parent.entity_set_name or parent.name
            for row in parent_raw_rows:
                pred = _key_predicate(row, parent_key_props)
                if not pred:
                    continue
                try:
                    raw_items.extend(
                        client.fetch_nav_collection(set_name, pred, prop.name))
                except OdataError as e:
                    stats["errors"].append(f"{child.name}({pred}): {e}")

        if not raw_items:
            continue

        res = _upsert_task(session, engine, child, raw_items, full=False, stats=stats)
        stats["fetched"] += len(raw_items)
        stats["inserted"] += res["inserted"]
        stats["updated"] += res["updated"]
        logger.info("коллекция %s.%s → %s: %d строк (fallback=%s)",
                    parent.name, prop.name, child.name, len(raw_items), used_fallback)


# ---------------------------------------------------------------------------
# основная оркестрация
# ---------------------------------------------------------------------------

def sync_entity(
    session: Session,
    engine: Engine,
    client: OdataClient,
    root_name: str,
    *,
    mode: str = "full",
    seed_keys: dict[str, list[str]] | None = None,
    trigger: str = "manual",
    job_id: int | None = None,
    on_task_done: Callable[[str, dict], None] | None = None,
) -> dict:
    """Загрузить сущность и все РАЗРЕШЁННЫЕ зависимости (staging→upsert).

    Порядок задач = BFS плана: корень (полный или по seed_keys), затем
    зависимости — только теми ключами, на которые ссылаются уже выгруженные
    строки (merge_seed_keys после каждой задачи). Возвращает статистику;
    ошибка одной задачи не прерывает остальные (частичная фиксация — ТЗ 4.2).
    """
    plan: LoadPlan = build_load_plan(session, root_name, mode=mode, seed_keys=seed_keys)
    stats = {"fetched": 0, "inserted": 0, "updated": 0, "deleted": 0,
             "tasks_ok": 0, "tasks_failed": 0,
             "skipped_forbidden": sorted(plan.skipped_forbidden),
             "warnings": [], "errors": []}

    for task in list(plan.tasks):
        entity = get_entity(session, task.entity_name)
        if entity is None or not is_globally_allowed(entity):
            continue
        run = _start_run(session, entity, trigger, job_id)
        try:
            key_props = [p for p in entity.properties if p.is_key]
            if not key_props:
                raise ValueError(f"у {entity.name} нет PK в реестре")

            filter_expr: str | None = None
            full = True
            if task.key_values is not None:
                keys = sorted(set(task.key_values))
                if not keys:
                    _finish_run(session, run, "skipped", {})
                    stats["tasks_ok"] += 1
                    continue
                kp = key_props[0]
                if len(key_props) > 1:
                    # составной PK: фильтр по первому ключу — избыточная выборка
                    # безопасна, upsert матчит по полному PK
                    logger.debug("%s: составной PK, фильтр по %s", entity.name, kp.name)
                filter_expr = _in_filter(kp.name, kp.value_type, keys)
                full = False  # частичная докачка: stale-строки НЕ удаляем

            set_name = entity.entity_set_name or entity.name
            rows = _fetch_all(client, set_name, filter_expr)

            res = _upsert_task(session, engine, entity, rows, full=full, stats=stats)
            _load_child_collections(session, engine, client, entity, rows, stats)

            # докачка зависимостей: distinct *_Key из выгруженных строк
            from odata_cache.services.load_plan import extract_ref_key_values

            extra: dict[str, list[str]] = {}
            for ref_id, vals in extract_ref_key_values(rows, entity).items():
                dep = session.get(Entity, ref_id)
                if dep is not None:
                    extra.setdefault(dep.name, []).extend(sorted(vals))
            merge_seed_keys(plan, {k: sorted(set(v)) for k, v in extra.items()})

            entity.last_synced_at = utcnow_iso()
            entity.last_sync_count = len(rows)
            counters = {"fetched": len(rows),
                        "inserted": res["inserted"], "updated": res["updated"],
                        "deleted": res["deleted"]}
            stats["fetched"] += counters["fetched"]
            stats["inserted"] += res["inserted"]
            stats["updated"] += res["updated"]
            stats["deleted"] += res["deleted"]
            stats["tasks_ok"] += 1
            _finish_run(session, run, "success", counters)
            if on_task_done:
                on_task_done(entity.name, counters)
        except Exception as e:  # noqa: BLE001 — одна задача не роняет прогон
            stats["tasks_failed"] += 1
            stats["errors"].append(f"{task.entity_name}: {e}")
            _finish_run(session, run, "failed", {"error": str(e)[:2000]})
            logger.exception("задача %s упала", task.entity_name)
    return stats


# ---------------------------------------------------------------------------
# sync_runs (лог для админки)
# ---------------------------------------------------------------------------

def _start_run(session: Session, entity: Entity, trigger: str,
               job_id: int | None) -> SyncRun:
    run = SyncRun(entity_id=entity.id, job_id=job_id, trigger=trigger,
                  status="running", started_at=utcnow_iso())
    session.add(run)
    session.flush()
    return run


def _finish_run(session: Session, run: SyncRun, status: str, counters: dict) -> None:
    run.status = status
    run.finished_at = utcnow_iso()
    run.fetched_count = int(counters.get("fetched", 0) or 0)
    run.inserted_count = int(counters.get("inserted", 0) or 0)
    run.updated_count = int(counters.get("updated", 0) or 0)
    run.deleted_count = int(counters.get("deleted", 0) or 0)
    if status == "failed":
        run.error_message = str(counters.get("error", ""))[:4000]
    run.details = json.dumps(counters, ensure_ascii=False, default=str)
    session.flush()
