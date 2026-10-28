"""Формат ответа data-API (согласован на шаге 7):

    {
      "data":     [ {...}, ... ],          # плоские строки «как в 1С» (кириллица)
      "sources":  { "<имя поля/связи>": {"entity": ..., "@source": ...} },
      "fields":   { "<field_name>": <описание типа> },
      "pagination": { ... },
      "warnings": [ ... ]
    }

Правила:
- служебные ключи OData (@odata.*, * @navigationLinkUrl) вырезаются;
- пустые GUID 00000000-0000-0000-0000-000000000000 → null (договорённость шага 8);
- даты — ISO8601; decimal — строкой (потеря точности недопустима);
- в каждой строке @source НЕ пишется — источник указан один раз в ``sources``.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from odata_cache.entity_info import _JSON_TYPE_BY_VALUE

ZERO_GUID = uuid.UUID(int=0).hex  # '00000000000000000000000000000000'


def json_type_of(value_type: str) -> str:
    """value_type реестра → JSON-тип для блока fields."""
    return _JSON_TYPE_BY_VALUE.get(value_type, "string")


def normalize_cell(value: Any) -> Any:
    """Привести значение колонки БД к JSON-совместимому виду."""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None  # NaN/Inf в JSON нельзя
    return value


def is_zero_guid(value: Any) -> bool:
    """Нулевой GUID 1С («ссылка не заполнена») — приводим к null."""
    if not isinstance(value, str):
        return False
    compact = value.replace("-", "").strip().lower()
    return compact == ZERO_GUID


def row_to_flat(row: dict[str, Any], *, zero_guid_to_null: bool = True) -> dict[str, Any]:
    """Строка ResultProxy → плоский dict без OData-служебщины."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if "@" in key or key.startswith("_"):
            continue  # страховка: служебные ключи OData не попадают в вывод
        if zero_guid_to_null and is_zero_guid(value):
            value = None
        out[key] = normalize_cell(value)
    return out


def build_sources(links: dict[str, dict]) -> dict[str, dict]:
    """Блок sources: имя связи → куда ведёт (+ @source при подмене alias/link_override)."""
    sources: dict[str, dict] = {}
    for base, entry in links.items():
        s: dict[str, Any] = {"entity": entry["entity"], "key_field": entry["key_field"]}
        if entry.get("@source"):
            s["@source"] = entry["@source"]
        sources[base] = s
    return sources


def build_fields(fields: dict[str, dict], links: dict[str, dict]) -> dict[str, dict]:
    """Блок fields: скалярные поля + *_Type ссылок (для динамических ссылок)."""
    out: dict[str, dict] = {}
    for name, desc in fields.items():
        d = desc.get("kind", "field")
        out[name] = {
            "type": desc.get("type", "string"),
            "nullable": desc.get("nullable", True),
            "key": desc.get("key", False),
            "kind": "collection" if d == "collection" else "field",
        }
    for base, entry in links.items():
        out[entry["key_field"]] = {"type": "uuid", "nullable": entry.get("nullable", True),
                                   "kind": "link", "entity": entry["entity"]}
        tf = entry.get("type_field")
        if tf:
            out[tf] = {"type": "string", "nullable": True, "kind": "link_type"}
    return out


def make_response(
    *,
    entity_name: str,
    rows: list[dict[str, Any]],
    fields: dict[str, dict],
    links: dict[str, dict],
    pagination: dict[str, Any] | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Собрать итоговый JSON ответа одной сущности."""
    return {
        "entity": entity_name,
        "data": [row_to_flat(r) for r in rows],
        "sources": build_sources(links),
        "fields": build_fields(fields, links),
        "pagination": pagination or {},
        "warnings": warnings or [],
    }
