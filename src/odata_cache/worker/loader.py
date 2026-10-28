"""Конвейер загрузки данных 1С в кеш-таблицы (шаг 8, часть 2).

Поток одной задачи LoadTask:
    сырой OData JSON (из сети или из fixture)
      -> normalize_rows()   чистка @odata.*, нормализация guid/дат/чисел,
                            вынос коллекций, пропуск неизвестных полей (OpenType)
      -> insert в staging (truncate + batch insert)
      -> upsert в боевую   (UPDATE существующих по PK + INSERT новых; один транзакт)
      -> при full-загрузке удаление устаревших строк боевой (нет в staging)

Модуль не знает про httpx — вход это «список dict'ов как в value[] ответа 1С».
Сетевой клиент подключается на шаге 9 (worker + расписание); тесты гоняют этот
слой на реальных сниппетах 1С без доступа к серверу.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import Table, and_, delete, or_, select, update
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_odata_annotation(key: str) -> bool:
    """@odata.metadata, Parent@navigationLinkUrl и т.п. — не данные."""
    return "@" in key or key.startswith("odata.")


def normalize_guid(value):
    """Guid 1С → канонический lowercase. Нулевой guid ('000...') храним как есть —
    фильтрация «пустых ссылок» важна. Не-guid → None."""
    if value is None:
        return None
    s = str(value).strip().lower()
    return s if _GUID_RE.match(s) else None


def _parse_dt(text: str) -> dt.datetime | None:
    t = text.strip()
    if not t:
        return None
    m = re.match(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$", t)  # старый формат OData v3
    if m:
        return dt.datetime.fromtimestamp(int(m.group(1)) / 1000, tz=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def coerce_value(value, value_type: str):
    """Привести JSON-значение поля к типу колонки; None если неприменимо."""
    if value is None or value == "":
        return None
    vt = value_type
    if vt == "guid":
        return normalize_guid(value)
    if vt == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "да")
    if vt == "int":
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None
    if vt == "float":
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return None
    if vt == "decimal":
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError):
            return None
    if vt in ("datetime", "date"):
        d = value if isinstance(value, (dt.date, dt.datetime)) else _parse_dt(str(value))
        if d is None:
            return None
        if isinstance(d, dt.datetime) and d.tzinfo is not None:
            d = d.replace(tzinfo=None)   # храним naive UTC-время как отдаёт 1С
        if vt == "date":
            return d.date() if isinstance(d, dt.datetime) else d
        if isinstance(d, dt.datetime):
            return d
        return dt.datetime(d.year, d.month, d.day)  # date → midnight datetime
    if isinstance(value, (dict, list)):
        return None  # вложенную структуру в скаляр не пишем
    return str(value)


def split_collections(rows: list[dict]) -> tuple[list[dict], dict[str, list[tuple]]]:
    """Вынуть массивы объектов («ДополнительныеРеквизиты», «RecordSet»):
    {prop: [(родительская_строка, элемент), ...]}. Аннотации удаляются везде."""
    children: dict[str, list] = {}
    clean: list[dict] = []
    for row in rows:
        c = {}
        for k, v in row.items():
            if is_odata_annotation(k):
                continue
            if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                children.setdefault(k, []).extend((row, item) for item in v)
            elif isinstance(v, list):
                continue  # массив примитивов вне схемы — режем
            else:
                c[k] = v
        clean.append(c)
    return clean, children


def normalize_rows(raw_rows: list[dict], columns: dict[str, str],
                   known_props: set[str]) -> tuple[list[dict], list[str]]:
    """rows → записи по колонкам таблицы. columns: имя→value_type.

    Поля, отсутствующие в реестре (OpenType-расширения), игнорируются с
    предупреждением; аннотации — молча.
    """
    warnings: list[str] = []
    out: list[dict] = []
    for row in raw_rows:
        rec: dict = {}
        for key, val in row.items():
            if is_odata_annotation(key):
                continue
            if key not in known_props:
                w = f"поле {key!r} не в схеме — пропущено"
                if w not in warnings:
                    warnings.append(w)
                continue
            if key in columns:
                rec[key] = coerce_value(val, columns[key])
        out.append(rec)
    return out, warnings


def load_batch(engine: Engine, staging: Table, battle: Table,
               rows: list[dict], *, key_cols: list[str], full: bool = True) -> dict:
    """Загрузить пакет: staging(truncate+insert) → upsert в battle → чистка stale.

    Возвращает счётчики {inserted, updated, deleted, warnings}.
    Все действия — в одной транзакции: боевая таблица никогда не видит
    полупакет (требование «временные таблицы → upsert»).
    """
    names = {c.name for c in battle.columns}
    warns: list[str] = []
    extra_seen: set[str] = set()
    aligned: list[dict] = []
    for r in rows:
        for e in sorted(set(r) - names):
            if e not in extra_seen and len(warns) < 5:
                extra_seen.add(e)
                warns.append(f"лишние поля отброшены: {e}")
        aligned.append({**{n: None for n in names},
                        **{k: v for k, v in r.items() if k in names}})

    keys_expr = [battle.c[k] for k in key_cols]
    with engine.begin() as conn:
        conn.execute(delete(staging))
        if aligned:
            conn.execute(staging.insert(), aligned)

        existing = {tuple(row) for row in conn.execute(select(*keys_expr)).all()}
        to_update, to_insert = [], []
        for r in aligned:
            (to_update if tuple(r[k] for k in key_cols) in existing else to_insert).append(r)

        for r in to_update:  # PK-колонки в payload не кладём
            payload = {c: v for c, v in r.items() if c not in key_cols}
            conn.execute(update(battle).where(*[battle.c[k] == r[k] for k in key_cols]), payload)
        if to_insert:
            conn.execute(battle.insert(), to_insert)

        deleted = 0
        if full and aligned:
            stage_keys = {tuple(r[k] for k in key_cols) for r in aligned}
            stale = existing - stage_keys
            if stale:
                if len(key_cols) == 1:
                    conn.execute(battle.delete().where(keys_expr[0].in_([s[0] for s in stale])))
                else:
                    conn.execute(battle.delete().where(
                        or_(*[and_(*[battle.c[k] == vals[i]
                                      for i, k in enumerate(key_cols)])
                              for vals in stale])))
                deleted = len(stale)

    return {"inserted": len(to_insert), "updated": len(to_update),
            "deleted": deleted, "warnings": warns}
