"""Кеш $metadata 1С в БД (шаг 3).

Храним сырые метаданные целиком (XML/JSON как отдаёт 1С) + хэш содержимого.
Воркер при старте и по расписанию сравнивает хэш: изменились — перепарсить
и обновить ``entity_properties`` / DDL боевых таблиц; не изменились — ничего
не делаем. Так «понятные таблицы» всегда соответствуют реальной схеме 1С,
а повторных запросов $metadata нет.

``MetadataSnapshot`` — история версий (для отладки «когда поехала схема»).
"""

from __future__ import annotations

import hashlib

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from odata_cache.db.base import Base
from odata_cache.models.common import utcnow_iso


class ODataMetadata(Base):
    """Текущая версия $metadata (одна активная строка, id=1 convention)."""

    __tablename__ = "odata_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fetched_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    service_root: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(32), default="xml", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @staticmethod
    def compute_hash(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class MetadataSnapshot(Base):
    """История изменений метаданных (append-only)."""

    __tablename__ = "odata_metadata_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)  # напр. "added entities: X,Y"
