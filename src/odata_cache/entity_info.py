"""Описание сущности для каталога и эндпоинта /api/v1/<entity>/info (шаг 7.3).

Модуль отвечает на вопрос «что можно запросить у этой таблицы» без обращения
к 1С: поля (с типами), связи (*_Key → куда ведут), ключ, количество записей
в кэше и допустимые операторы фильтра по каждому полю.

Формат рассчитан и на человека, и на AI-агента: имена — как в 1С (кириллица
сохраняется), ссылки дают готовый пример $filter, цели ссылок уже пропущены
через разрешение alias/link_override (т.е. показывается РЕАЛЬНЫЙ источник).

Зависит только от реестра (entities/entity_properties) и сервиса разрешения
сущностей — к данным 1С не обращается.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, func, inspect, select, text
from sqlalchemy.orm import Session

from odata_cache.db.base import Base
from odata_cache.models.registry import Entity, EntityProperty
from odata_cache.services.access import is_globally_allowed
from odata_cache.services.entity_resolution import ResolvedRef, resolve_for_plan
from odata_cache.services.registry_service import ref_property_base

# ---------------------------------------------------------------------------
# Словарь типов/операторов (см. docs/03_FILTER.md, решение Q4-a)
# ---------------------------------------------------------------------------

# Операторы, доступные фильтру для значения типа колонки БД (value_type).
_OPERATORS_BY_TYPE: dict[str, list[str]] = {
    "string": ["=", "<>", "~", "contains", "startswith", "endswith", "in", "is null"],
    "guid": ["=", "<>", "in", "is null"],
    "int": ["=", "<>", ">", ">=", "<", "<=", "in", "is null"],
    "float": ["=", "<>", ">", ">=", "<", "<=", "in", "is null"],
    "decimal": ["=", "<>", ">", ">=", "<", "<=", "in", "is null"],
    "bool": ["=", "<>", "in", "is null"],
    "date": ["=", "<>", ">", ">=", "<", "<=", "is null"],
    "datetime": ["=", "<>", ">", ">=", "<", "<=", "is null"],
    # структурные/коллекционные поля через $filter не фильтруются
}

# value_type → JSON-тип в выводе (чтобы агент мог валидировать значения).
_JSON_TYPE_BY_VALUE: dict[str, str] = {
    "string": "string",
    "guid": "uuid",
    "int": "integer",
    "float": "number",
    "decimal": "number",
    "bool": "boolean",
    "date": "date",
    "datetime": "date-time",
}


def _column_kind(prop: EntityProperty) -> str:
    """'link' | 'collection' | 'field' — роль свойства в выводе info."""
    if prop.ref_kind == "collection":
        return "collection"
    if prop.ref_kind == "ref" or (prop.name.endswith("_Key") and prop.ref_entity_id is not None):
        return "link"
    return "field"


def _entity_by_id(session: Session, entity_id: int | None) -> Entity | None:
    if entity_id is None:
        return None
    return session.get(Entity, entity_id)


def field_descriptors(
    session: Session,
    entity: Entity,
    resolved: dict[str, ResolvedRef],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Разложить EntityProperty реестра на (fields, links).

    * fields — плоские колонки данных (только exposed); служебные *_Type,
      стоящие рядом со ссылкой, в fields не попадают (они уходят в link.type_field);
    * links  — навигационные ссылки: базовое имя (`Номенклатура`) ведёт на
      `_Key`-колонку; цель разрешена через alias/link_override, при подмене
      добавляется `@source`.
    """
    fields: dict[str, dict] = {}
    links: dict[str, dict] = {}

    props = sorted(entity.properties, key=lambda p: (p.position, p.name))
    by_name = {p.name: p for p in props}

    # 1) ссылки — сначала, чтобы знать, какие *_Type «занятые»
    for p in props:
        if not p.exposed or _column_kind(p) != "link":
            continue
        base = ref_property_base(p.name)  # «Номенклатура_Key» → «Номенклатура»
        r = resolved.get(p.name)
        target_id = r.effective_id if r is not None else p.ref_entity_id
        target = _entity_by_id(session, target_id)
        # цель запрещена (is_active/cacheable=False) или неизвестна → ссылка НЕ
        # попадает в вывод; поле остаётся обычным guid-полем без семантики связи
        if target is None or not is_globally_allowed(target):
            desc = _scalar_descriptor(p)
            fields[p.name] = desc
            continue

        entry: dict[str, Any] = {
            "kind": "link",
            "key_field": p.name,
            "entity": target.name,
            "cardinality": "many_to_one",
            "nullable": p.nullable,
            "example_filter": f"{p.name} eq {{guid}}",
        }
        if r is not None and r.changed:
            entry["@source"] = r.rule  # 'override' | 'alias_prop' | 'alias_owner' | 'alias_global'
        type_prop = by_name.get(f"{base}_Type")
        if type_prop is not None and type_prop.exposed:
            entry["type_field"] = type_prop.name  # динамические ссылки (Recorder_Type и т.п.)
        if target.title_property:
            entry["target_title_field"] = target.title_property
        links[base] = entry

    # 2) скалярные поля
    linked_type_fields = {e["type_field"] for e in links.values() if "type_field" in e}
    for p in props:
        if not p.exposed or p.name in linked_type_fields:
            continue
        kind = _column_kind(p)
        if kind == "link":
            continue  # уже в links
        desc = _scalar_descriptor(p)
        if kind == "collection":
            desc["kind"] = "collection"
            if p.ref_entity_id is not None:
                t = _entity_by_id(session, p.ref_entity_id)
                desc["item_entity"] = t.name if t else None
        fields[p.name] = desc

    return fields, links


def _scalar_descriptor(p: EntityProperty) -> dict[str, Any]:
    desc: dict[str, Any] = {
        "kind": "field",
        "type": _JSON_TYPE_BY_VALUE.get(p.value_type, "string"),
        "value_type": p.value_type,
        "edm_type": p.edm_type,
        "nullable": p.nullable,
        "key": bool(p.is_key),
        "title": bool(p.is_title),
        "operators": _OPERATORS_BY_TYPE.get(p.value_type, []),
    }
    if p.max_length:
        desc["max_length"] = p.max_length
    return desc





def cache_stats(session: Session, entity: Entity) -> dict:
    """Количество записей в таблице-кэше; rows=None, если таблица не создана."""
    table_name = entity.storage_table
    bind = session.get_bind()
    if table_name not in inspect(bind).get_table_names():
        return {"rows": None, "table_exists": False}
    if table_name in Base.metadata.tables:
        tbl: Table = Base.metadata.tables[table_name]
        rows = session.scalar(select(func.count()).select_from(tbl))
    else:
        rows = session.scalar(text(f'SELECT COUNT(*) FROM "{table_name}"'))
    return {"rows": rows, "table_exists": True}


def entity_info(session: Session, entity: Entity, *, with_stats: bool = True) -> dict:
    """Полное описание одной кэшируемой сущности (для /<entity>/info)."""
    resolved = resolve_for_plan(session, entity)
    fields, links = field_descriptors(session, entity, resolved)
    out: dict[str, Any] = {
        "name": entity.name,
        "state": entity.state,
        "cacheable": entity.cacheable,
        "odata_type": entity.odata_type_name,
        "storage_table": entity.storage_table,
        "key_property": entity.key_property,
        "title_field": entity.title_property,
        "fields": fields,
        "links": links,
        "last_synced_at": entity.last_synced_at,
        "last_sync_count": entity.last_sync_count,
    }
    if entity.alias_of_id is not None:
        alias_src = session.get(Entity, entity.alias_of_id)
        out["alias_of"] = alias_src.name if alias_src else None
    if with_stats:
        out["stats"] = cache_stats(session, entity)
    out["filterable_fields"] = [
        {"field": name, "operators": d["operators"]}
        for name, d in sorted(fields.items())
        if d.get("operators")
    ]
    return out


def build_catalog(session: Session, entities: list[Entity]) -> list[dict]:
    """Краткий список доступных сущностей (для GET /api/v1/entities)."""
    items = []
    for e in entities:
        resolved = resolve_for_plan(session, e)
        fields, links = field_descriptors(session, e, resolved)
        items.append(
            {
                "name": e.name,
                "state": e.state,
                "title_field": e.title_property,
                "record_count": e.last_sync_count,
                "last_synced_at": e.last_synced_at,
                "fields": len(fields),
                "links": len(links),
            }
        )
    return items
