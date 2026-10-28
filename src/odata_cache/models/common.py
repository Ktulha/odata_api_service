"""Общие типы и миксины для моделей (шаг 3).

``UTCText`` — TEXT, который всегда хранит время в UTC (ISO-8601 строки с
смещением ``+00:00``). Выбор TEXT вместо нативного TIMESTAMP продиктован
требованием одинаково работать на PostgreSQL и SQLite без потерь точности
и таймзон; сортировка лексикографически корректна для ISO-формата.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column


def utcnow_iso() -> str:
    """Текущее время в UTC, формат ISO-8601 (секунды с микросекундами)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class UTCText(TypeDecorator[str]):
    """TEXT с нормализацией значения в UTC-ISO при записи."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:  # считаем naive-данные UTC
                value = value.replace(tzinfo=timezone.utc)
            value = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
        return str(value)


class TimestampMixin:
    """created_at / updated_at — заполняются ORM, не триггерами (переносимость)."""

    created_at: Mapped[str] = mapped_column(UTCText, default=utcnow_iso, nullable=False)
    updated_at: Mapped[str] = mapped_column(
        UTCText, default=utcnow_iso, onupdate=utcnow_iso, nullable=False
    )
