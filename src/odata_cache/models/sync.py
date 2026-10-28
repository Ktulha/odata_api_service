"""Журнал синхронизаций и задач расписания (шаг 3).

``sync_job`` — настройка расписания для одной сущности (cron), редактируется
из админки; воркер читает эти строки при (пере)постановке задач APScheduler.

``sync_run`` — факт одного прогона: stage → upsert, итоговый статус, счётчики
и текст ошибки. Это основной «лог» синхронизации для админки (просмотр логов)
и для отладки расхождений с 1С.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from odata_cache.db.base import Base
from odata_cache.models.common import TimestampMixin

SYNC_STATUSES = ("pending", "running", "success", "partial", "failed", "skipped")


class SyncJob(TimestampMixin, Base):
    """Расписание синхронизации сущности."""

    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cron: Mapped[str] = mapped_column(String(64), nullable=False)  # напр. "*/15 * * * *"
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    # параметры выгрузки из 1С
    batch_size: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    max_retries: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # если True — в staging тянутся только изменённые с прошлого прогона записи
    # (реализация на шаге 6/8; по умолчанию полная выгрузка)
    incremental: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_run_at: Mapped[str | None] = mapped_column(Text)
    next_run_at: Mapped[str | None] = mapped_column(Text)

    entity: Mapped["Entity"] = relationship()  # noqa: F821
    runs: Mapped[list[SyncRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SyncJob entity={self.entity_id} cron={self.cron!r}>"


class SyncRun(Base):
    """Один прогон синхронизации (ручной или по расписанию)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )

    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule", nullable=False)
    # 'schedule' | 'manual' | 'startup'
    started_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)

    fetched_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    error_message: Mapped[str | None] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)  # JSON: стектрейсы, статистика по партиям

    job: Mapped[SyncJob | None] = relationship(back_populates="runs")
    entity: Mapped["Entity"] = relationship()  # noqa: F821

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SyncRun entity={self.entity_id} {self.status}>"


class RequestLog(Base):
    """Безопасный аудит вызовов data-API (пишется асинхронно, шаг 11).

    Никаких секретов: токен идентифицируется дайджестом/именем, запросные
    строки пишутся как есть (они не содержат секретов), тело отсутствует —
    GET-метод.
    """

    __tablename__ = "request_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[str] = mapped_column(Text, nullable=False)
    token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL")
    )
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("roles.id", ondelete="SET NULL")
    )
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    entity_name: Mapped[str | None] = mapped_column(String(128))
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    client_ip: Mapped[str | None] = mapped_column(String(64))


class LazyLoadRequest(Base):
    """Очередь ленивой загрузки: заявка API → исполнитель worker (шаг C.5).

    Когда клиент впервые обращается к разрешённой, но ещё ни разу не
    синхронизированной сущности, api-процесс НЕ ходит в 1С сам — он ставит
    сюда pending-заявку и отвечает пустым набором с warning'ом. Worker
    (отдельный процесс или in-process планировщик в режиме 'all') подбирает
    заявки своим циклом и выполняет sync_entity(trigger='startup').
    Дедупликация на стороне api: одна pending/running-заявка на сущность.
    """

    __tablename__ = "lazy_load_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # pending → running → done | failed
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    claimed_at: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)

    entity: Mapped["Entity"] = relationship()  # noqa: F821
