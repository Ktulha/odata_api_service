"""Компилятор AST $filter → SQLAlchemy (шаг 7.2).

Вход: AST из ``filter_dsl.parse_filter`` + описание полей сущности
(``EntityProperty`` из реестра). Выход: ``sqlalchemy.BinaryExpression``,
который можно передать в ``where()`` динамического select по таблице-кэшу.

Правила (решение Q4-b, вариант «а»):
  * имена полей сопоставляются без учёта регистра; не exposed / неизвестное
    имя → ``FilterCompileError`` (HTTP 400);
  * приведение литералов — по типу ПОЛЯ (колонка первична): строковый литерал
    для числового поля парсится, GUID приводится к каноническому нижнему
    регистру, bool принимает true/false/'1'/'0'; невозможное приведение → 400;
  * сравнения строк/дат — через типизированные bind-параметры (без подстановки);
  * ``~`` / contains / startswith / endswith → LIKE c экранированием % и _
    (ESCAPE '\\'); регистронезависимость обеспечивается lower()-ом обеих сторон
    на уровне SQL (работает одинаково в PostgreSQL и SQLite);
  * guid-поля хранятся как текст: значения нормализуются (скобки guid(...)
    снимаются, регистр — нижний), поэтому сравнения корректны и для NULL-
    нормализованных пустых GUID их нет — они загружаются как NULL (см. загрузку).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.elements import BinaryExpression

from odata_cache.filter_dsl import (
    Compare,
    FilterSyntaxError,
    InList,
    IsNull,
    Literal,
    Logic,
    Node,
    Not,
    parse_filter,
)

# ---------------------------------------------------------------------------
# Ошибки компиляции (все → HTTP 400 с позицией, если она известна)
# ---------------------------------------------------------------------------


class FilterCompileError(ValueError):
    """Ошибка семантики фильтра: неизвестное поле, тип, синтаксис."""

    def __init__(self, message: str, pos: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.pos = pos

    def as_dict(self) -> dict:
        out: dict = {"code": "filter_error", "message": self.message}
        if self.pos is not None:
            out["pos"] = self.pos
        return out


# ---------------------------------------------------------------------------
# Описание поля (независимо от ORM — чтобы компилятор можно было тестировать
# и с реальными EntityProperty, и с лёгкими заглушками)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldInfo:
    name: str            # имя как в 1С/API
    column: str          # физическое имя колонки в таблице-кэше
    value_type: str      # string|guid|int|float|decimal|bool|date|datetime
    exposed: bool = True
    column_obj: object | None = None  # SQLAlchemy Column (заполняет make_field_infos)
    ci: bool | None = None            # принудительная регистронезависимость; None → авто по диалекту


def field_info_from_property(prop) -> FieldInfo:
    """Адаптер ORM EntityProperty → FieldInfo."""
    return FieldInfo(
        name=prop.name,
        column=getattr(prop, "column_name", prop.name),
        value_type=prop.value_type,
        exposed=bool(prop.exposed),
    )


def build_field_index(props: list) -> dict[str, FieldInfo]:
    """Индекс «имя (lower) → FieldInfo»; ключевые слова не конфликтуют —
    поиск идёт только по реально существующим полям."""
    index: dict[str, FieldInfo] = {}
    for p in props:
        fi = p if isinstance(p, FieldInfo) else field_info_from_property(p)
        index[fi.name.lower()] = fi
    return index


# ---------------------------------------------------------------------------
# Приведение литералов по типу поля
# ---------------------------------------------------------------------------

_GUID_RE = re.compile(
    r"""^[\(\{\[]?guid['"]?\s*\(?(.*?)\)?['"]?[\)\}\]]?$""",
    re.I | re.S,
)
_DATE_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})([T ]\d{2}:\d{2}(:\d{2})?)?")


def normalize_guid(value: str) -> str | None:
    """Канонический вид GUID для хранения/сравнения (нижний регистр, без скобок)."""
    m = _GUID_RE.match(value.strip())
    raw = m.group(1) if m else value.strip()
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


def _parse_number(text: str) -> float | int | None:
    try:
        if any(ch in text for ch in (".", "e", "E")):
            return float(text)
        return int(text)
    except ValueError:
        return None


def _parse_datetime(text: str) -> date | datetime | None:
    """Парсит ISO-префикс дат 1С/OData: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS(.ffffff)."""
    m = _DATE_FULL_RE.match(text.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        day = date(y, mo, d)
    except ValueError:
        return None
    time_part = m.group(4)
    if time_part is None:
        return day
    hh, mm = int(time_part[1:3]), int(time_part[4:6])
    ss = int(time_part[7:9]) if len(time_part) >= 9 and time_part[6] == ":" else 0
    frac = 0
    fm = re.search(r"\.(\d+)", text)
    if fm:
        frac = int((fm.group(1) + "000000")[:6])
    return datetime(y, mo, d, hh, mm, ss, frac)


def coerce(value, vt: str, *, op: str, field: str, pos: int | None):
    """Привести литерал к типу поля; при неудаче — FilterCompileError.

    ``value`` может быть ``Literal`` (префиксный тип из $filter: guid'...' и
    т.п.) — тогда объявленный тип проверяется строго независимо от типа поля.
    Для строковых полей с like-операторами приведение типов не делается вовсе
    (число/дата сравниваются как текст).
    """
    if value is None:
        return None
    hint = None
    if isinstance(value, Literal):
        hint = value.type_hint
        value = str(value)
    like_ops = ("contains", "startswith", "endswith")
    if hint == "guid":
        g = normalize_guid(str(value))
        if g is None:
            raise FilterCompileError(f"не GUID '{value}' (литерал guid)", pos)
        return g
    if hint in ("date", "datetime"):
        dt = _parse_datetime(str(value))
        if dt is None:
            raise FilterCompileError(
                f"не дата '{value}' (формат YYYY-MM-DD[THH:MM:SS])", pos)
        if hint == "date":
            return dt if isinstance(dt, date) and not isinstance(dt, datetime) \
                else dt.date()
        if isinstance(dt, date) and not isinstance(dt, datetime):
            return datetime(dt.year, dt.month, dt.day)
        return dt
    match vt:
        case "int" | "float" | "decimal":
            if op in like_ops:      # подстрока ищется по текстовому виду
                return str(value)
            if isinstance(value, bool):
                raise FilterCompileError(
                    f"поле '{field}' числовое, ожидается число", pos)
            if isinstance(value, (int, float)):
                return value
            num = _parse_number(str(value))
            if num is None:
                raise FilterCompileError(
                    f"не число '{value}' для поля '{field}' ({vt})", pos)
            return num
        case "bool":
            if op in like_ops:
                return str(value)
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("true", "1"):
                return True
            if s in ("false", "0"):
                return False
            raise FilterCompileError(
                f"ожидается true/false для поля '{field}'", pos)
        case "guid":
            g = normalize_guid(str(value))
            if g is None:
                raise FilterCompileError(
                    f"не GUID '{value}' для поля '{field}'", pos)
            return g
        case "date" | "datetime":
            if op in like_ops:
                return str(value)
            dt = _parse_datetime(str(value))
            if dt is None:
                raise FilterCompileError(
                    f"не дата '{value}' (формат YYYY-MM-DD[THH:MM:SS]) "
                    f"для поля '{field}'", pos)
            if vt == "date":
                return dt if isinstance(dt, date) and not isinstance(dt, datetime) \
                    else dt.date()
            # datetime: «голая» дата без времени трактуется как 00:00:00
            if isinstance(dt, date) and not isinstance(dt, datetime):
                return datetime(dt.year, dt.month, dt.day)
            return dt
        case _:  # string и всё остальное
            return str(value)


# ---------------------------------------------------------------------------
# Компиляция
# ---------------------------------------------------------------------------

_LIKE_ESCAPE = "\\"


def _like_expr(column, pattern: str) -> BinaryExpression:
    return column.like(pattern, escape=_LIKE_ESCAPE)


def _escape_like(s: str) -> str:
    return (s.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
             .replace("%", _LIKE_ESCAPE + "%")
             .replace("_", _LIKE_ESCAPE + "_"))


def _ci_pattern(needle: str) -> str:
    """Шаблон LIKE без учёта регистра для PostgreSQL: [aA]-классы для всех букв
    (ASCII и кириллица). Спецсимволы экранируются. Классы не зависят от
    collation/ICU, поэтому работают детерминированно в любой БД-локали."""
    out = []
    for ch in needle:
        if ch in (_LIKE_ESCAPE, "%", "_"):     # спецсимвол — экранируем как есть
            out.append(_LIKE_ESCAPE + ch)
            continue
        low, up = ch.lower(), ch.upper()
        out.append(f"[{low}{up}]" if low != up else ch)
    return "".join(out)


def _dialect_of(column) -> str | None:
    """Имя диалекта соединения, привязанного к колонке/таблице.

    Порядок поиска (SQLAlchemy 2.x):
      1. ``column.table.metadata.bind`` — MetaData, загружавшая таблицу через
         ``autoload_with=engine`` (основной путь для query_service);
      2. ``column.table.metadata._engine`` — «привязка» через Table.create;
      3. глобальный engine процесса из ``db.engine.get_engine()`` — production-
         путь для ORM-таблиц, созданных общим движком.
    Если ни один источник не найден — возвращаем None (fallback на [aA]-классы,
    безопасно и для PostgreSQL, и для SQLite с ASCII-данными).
    """
    table = getattr(column, "table", None)
    md = getattr(table, "metadata", None) if table is not None else None
    eng = getattr(md, "bind", None) if md is not None else None
    if eng is None and md is not None:
        eng = getattr(md, "_engine", None)
    if eng is None:
        # Глобальный engine процесса (ленивый импорт — чтобы компилятор можно было
        # использовать без БД, например в чистых unit-тестах AST).
        try:
            from odata_cache.db import engine as _db_engine
            eng = _db_engine._engine
        except Exception:  # pragma: no cover - защита от циклических импортов
            eng = None
    if eng is not None:
        return getattr(getattr(eng, "dialect", None), "name", None)
    return None


def _like_ci(column, pattern: str, ci: bool | None = None) -> BinaryExpression:
    """Сопоставление с ГОТОВЫМ шаблоном LIKE без учёта регистра.

    pattern может содержать % и _ (метасимволы сохраняются как есть): шаблон
    собрали компилятор (contains/startswith/endswith) или пользователь
    («~ '%слово%'» / like с wildcards).
    ci=False → обычный LIKE (коллация БД решает).

    Реализация выбирается по диалекту соединения (column.table.db.dialect):
      * SQLite — u_lower(col) LIKE u_lower(pattern): встроенные lower/LIKE в
        SQLite знают только ASCII, поэтому на connect регистрируется
        Python-функция u_lower (см. db/engine.py);
      * остальные (PostgreSQL) — [aA]-классы, не зависят от collation.
    Индексы не используются, но поиск подстроки их и так не использует;
    для кэша 1С это приемлемо.
    """
    if ci is False:
        return _like_expr(column, pattern)
    if _dialect_of(column) == "sqlite":
        ul = func.u_lower
        return ul(column).like(ul(pattern), escape=_LIKE_ESCAPE)
    return _like_expr(column, _ci_pattern(pattern))


def compile_node(node: Node, fields: dict[str, FieldInfo]):
    """AST → SQLAlchemy criterion (bind-параметризованный)."""
    match node:
        case Compare():
            fi = _resolve_field(node.field_name, fields, node.pos)
            col = _col(fi)
            val = coerce(node.value, fi.value_type,
                         op=node.op, field=fi.name, pos=node.pos)
            match node.op:
                case "eq":
                    return col == val
                case "ne":
                    return col != val
                case "gt":
                    return col > val
                case "ge":
                    return col >= val
                case "lt":
                    return col < val
                case "le":
                    return col <= val
                case "contains":
                    # «~» / like / contains: если пользователь сам задал шаблон
                    # с % — используем его как есть (экранируем только буквы под ci);
                    # иначе — ищем подстроку.
                    raw = str(val)
                    if "%" in raw or "_" in raw:
                        pattern = raw
                    else:
                        pattern = f"%{_escape_like(raw)}%"
                    return _like_ci(col, pattern, ci=fi.ci)
                case "startswith":
                    return _like_ci(col, f"{_escape_like(str(val))}%", ci=fi.ci)
                case "endswith":
                    return _like_ci(col, f"%{_escape_like(str(val))}", ci=fi.ci)
                case _:
                    raise FilterCompileError(f"оператор '{node.op}' не поддерживается", node.pos)

        case IsNull():
            fi = _resolve_field(node.field_name, fields, node.pos)
            col = _col(fi)
            return col.isnot(None) if node.negated else col.is_(None)

        case InList():
            fi = _resolve_field(node.field_name, fields, node.pos)
            col = _col(fi)
            vals = [coerce(v, fi.value_type, op="in", field=fi.name, pos=node.pos)
                    for v in node.items]
            if not vals:
                # пустой список: `in ()` — ложь, `not in ()` — истина
                from sqlalchemy import false, true
                return true() if node.negated else false()
            expr = col.in_(vals)
            return ~expr if node.negated else expr

        case Logic():
            parts = [compile_node(c, fields) for c in node.children]
            return and_(*parts) if node.op == "and" else or_(*parts)

        case Not():
            return ~compile_node(node.child, fields)

        case _:
            raise FilterCompileError(f"узел AST '{type(node).__name__}' не поддерживается")


def _resolve_field(name: str, fields: dict[str, FieldInfo], pos: int | None) -> FieldInfo:
    fi = fields.get(name.lower())
    if fi is None:
        raise FilterCompileError(f"неизвестное поле '{name}'", pos)
    if not fi.exposed:
        raise FilterCompileError(f"поле '{name}' недоступно", pos)
    return fi


def _col(fi: FieldInfo):
    col = fi.column_obj
    if col is not None:
        return col
    raise FilterCompileError(
        f"поле '{fi.name}' отсутствует в таблице-кэше (схема не синхронизирована)")


# ---------------------------------------------------------------------------
# Публичные хелперы
# ---------------------------------------------------------------------------

def make_field_infos(table, props: list) -> dict[str, FieldInfo]:
    """Индекс полей: ORM-описание + реальные колонки dynamic-таблицы."""
    index: dict[str, FieldInfo] = {}
    for p in props:
        fi = p if isinstance(p, FieldInfo) else field_info_from_property(p)
        col = table.c.get(fi.column)
        if col is None:
            col = table.c.get(fi.name)
        if col is None:
            continue  # свойства может ещё не быть в таблице (схема рассинхрон)
        index[fi.name.lower()] = FieldInfo(
            name=fi.name, column=fi.column, value_type=fi.value_type,
            exposed=fi.exposed, column_obj=col,
        )
    return index


def compile_filter(source: str, fields: dict[str, FieldInfo]):
    """Строка $filter → критерий SQLAlchemy. Синтаксис → FilterCompileError."""
    try:
        ast = parse_filter(source)
    except FilterSyntaxError as e:
        raise FilterCompileError(e.message, e.position) from e
    return compile_node(ast, fields)
