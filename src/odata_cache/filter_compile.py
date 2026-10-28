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
    """Привести литерал к типу поля; при неудаче — FilterCompileError."""
    if value is None:
        return None
    match vt:
        case "int" | "float" | "decimal":
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


def _ci_pattern(pattern: str) -> str:
    """Регистронезависимый LIKE без lower() на колонке — иначе индекс не
    используется. ASCII-часть паттерна разворачиваем в [Aa]-классы, кириллица
    в SQLite и PostgreSQL сопоставляется LIKE как есть (NOCASE/ICU/collation)."""
    out = []
    for ch in pattern:
        if "a" <= ch.lower() <= "z":
            out.append(f"[{ch.lower()}{ch.upper()}]")
        else:
            out.append(ch)
    return "".join(out)


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
                    return _like_expr(col, _ci_pattern(f"%{_escape_like(str(val))}%"))
                case "startswith":
                    return _like_expr(col, _ci_pattern(f"{_escape_like(str(val))}%"))
                case "endswith":
                    return _like_expr(col, _ci_pattern(f"%{_escape_like(str(val))}"))
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
