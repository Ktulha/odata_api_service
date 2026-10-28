"""Сервис чтения кэша для data-API (шаг 10.3).

Все запросы клиентов выполняет ТОЛЬКО этот модуль — к 1С он не обращается
(принцип ТЗ: API читает только собственную БД). Разрешение имени сущности
учитывает alias/link_override: если таблица-прокладка пустая, данные читаются
из реальной таблицы-источника; в ответе это видно в блоке ``sources``.

Поддерживаемые query-параметры (согласовано на шаге 7):
  $filter / filter   — DSL из docs/03_FILTER.md (см. filter_dsl/filter_compile);
  $select / select   — список полей через запятую (только exposed-колонки);
  $orderby/orderby   — «Поле [asc|desc], …»;
  $top/top           — размер страницы (по умолчанию/максимум — из Settings);
  $skip/skip         — смещение;
  count              — 1/true → посчитать всего записей (иначе total=null).
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.orm import Session

from odata_cache.config import Settings, get_settings
from odata_cache.entity_info import field_descriptors
from odata_cache.filter_compile import FilterCompileError, compile_filter, make_field_infos
from odata_cache.response_format import make_response
from odata_cache.services.access import AccessError, require_entity_access
from odata_cache.services.entity_resolution import resolve_for_plan

# имя поля: Unicode-буквы/цифры/подчёркивание (кириллица разрешена)
_ORDER_TOKEN_RE = re.compile(r"^\w+$", re.UNICODE)


class QueryError(Exception):
    """Клиентская ошибка запроса к data-API → HTTP 400."""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


def _load_table(session: Session, table_name: str) -> Table | None:
    """Dynamic Table по имени из БД (None, если таблица ещё не создана)."""
    bind = session.get_bind()
    if not inspect(bind).has_table(table_name):
        return None
    meta = MetaData()
    return Table(table_name, meta, autoload_with=bind)


def _pick_int(params: dict[str, str], *keys: str, default: int | None) -> int | None:
    for k in keys:
        if k in params and params[k] != "":
            try:
                return int(params[k])
            except ValueError:
                raise QueryError(f"параметр {k} должен быть целым числом") from None
    return default


def parse_orderby(source: str, fields: dict) -> list:
    """«Description asc, Code desc» → список SQLAlchemy order_by-выражений."""
    clauses = []
    for token in source.split(","):
        parts = token.strip().split()
        if not parts:
            continue
        name = parts[0]
        direction = parts[1].lower() if len(parts) > 1 else "asc"
        if direction not in ("asc", "desc"):
            raise QueryError(f"$orderby: неверное направление '{direction}' у поля '{name}'")
        fi = fields.get(name.lower())
        if fi is None:
            # допускаем сортировку и по служебным колонкам реестра (например *_Type)
            raise QueryError(f"$orderby: неизвестное поле '{name}'")
        if not fi.exposed:
            raise QueryError(f"$orderby: поле '{name}' недоступно")
        col = fi.column_obj
        if col is None:
            raise QueryError(f"$orderby: поле '{name}' отсутствует в таблице-кэше")
        clauses.append(col.desc() if direction == "desc" else col.asc())
    if not clauses:
        raise QueryError("$orderby: пустое выражение")
    return clauses


def read_entity(
    session: Session,
    *,
    entity_name: str,
    role_id: int | None,
    params: dict[str, str],
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Обработать GET /api/v1/<entity>. Вернуть готовый JSON-структуру ответа.

    Бросает AccessError (403/404) и QueryError (400) — HTTP-слой их конвертирует.
    """
    settings = settings or get_settings()

    entity = require_entity_access(session, name=entity_name, role_id=role_id)

    # куда реально смотрим: своя таблица или источник через alias/link_override
    resolved = resolve_for_plan(session, entity)
    table = _load_table(session, entity.storage_table)
    warnings: list[dict[str, str]] = []
    if table is None:
        # таблица не создана (сущность активна, но данные ещё не грузились) —
        # пустой результат вместо ошибки: ленивая загрузка включится на worker'е
        warnings.append({"code": "not_cached",
                         "message": "данные сущности ещё не загружены в кеш"})
        fields_desc, links_desc = field_descriptors(session, entity, resolved)
        return make_response(entity_name=entity.name, rows=[], fields=fields_desc,
                             links=links_desc,
                             pagination={"skip": 0, "top": 0, "returned": 0,
                                         "total": 0, "has_more": False},
                             warnings=warnings)

    props = list(entity.properties)
    findex = make_field_infos(table, props)
    fields_desc, links_desc = field_descriptors(session, entity, resolved)

    # --- $filter -------------------------------------------------------------
    criteria = None
    filt = params.get("$filter") or params.get("filter")
    if filt:
        try:
            criteria = compile_filter(filt, findex)
        except FilterCompileError as e:
            raise QueryError(e.message, detail=e.as_dict()) from e

    # --- $select --------------------------------------------------------------
    selected_cols = None
    sel = params.get("$select") or params.get("select")
    if sel:
        cols = []
        for raw in sel.split(","):
            name = raw.strip()
            if not name:
                continue
            fi = findex.get(name.lower())
            if fi is None:
                raise QueryError(f"$select: неизвестное поле '{name}'")
            if not fi.exposed:
                raise QueryError(f"$select: поле '{name}' недоступно")
            cols.append(fi.column_obj)
        if not cols:
            raise QueryError("$select: пустой список полей")
        selected_cols = cols

    # --- пагинация ------------------------------------------------------------
    top = _pick_int(params, "$top", "top", default=settings.default_page_size)
    skip = _pick_int(params, "$skip", "skip", default=0)
    assert top is not None and skip is not None
    if top < 0 or skip < 0:
        raise QueryError("$top/$skip не могут быть отрицательными")
    if top > settings.max_page_size:
        raise QueryError(f"$top превышает максимум {settings.max_page_size}")

    stmt = select(*(selected_cols or table.columns))
    if criteria is not None:
        stmt = stmt.where(criteria)

    # сортировка: явная или дефолтная (PK — чтобы страница была детерминированной)
    ord_src = params.get("$orderby") or params.get("orderby")
    if ord_src:
        order = parse_orderby(ord_src, findex)
    else:
        order = [c.asc() for c in table.primary_key.columns]
    stmt = stmt.order_by(*order).offset(skip).limit(top)

    rows = [dict(r._mapping) for r in session.execute(stmt)]

    # total считаем только по явному запросу (COUNT дорог на больших таблицах)
    want_count = (params.get("count") or "").strip().lower() in ("1", "true", "yes")
    total = None
    if want_count:
        from sqlalchemy import func

        total = session.scalar(select(func.count()).select_from(stmt.subquery()))

    return make_response(
        entity_name=entity.name,
        rows=rows,
        fields=fields_desc,
        links=links_desc,
        pagination={
            "skip": skip,
            "top": top,
            "returned": len(rows),
            "total": total,
            "has_more": (len(rows) == top),
        },
        warnings=warnings,
    )
