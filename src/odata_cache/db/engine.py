"""Engine / session-инфраструктура (шаг 3).

Один процесс = один engine (лениво, на первый запрос). Поддерживаются
PostgreSQL и SQLite; для SQLite автоматически включается WAL и внешние
ключи, а также ``check_same_thread=False`` — фоновый планировщик воркера
работает в отдельных потоках.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, pool
from sqlalchemy.orm import Session, sessionmaker

from odata_cache.config import Settings, get_settings
from odata_cache.db.base import Base

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(settings: Settings) -> Engine:
    url = settings.database_url
    kwargs: dict = {"future": True, "echo": settings.sql_echo}

    if url.startswith("sqlite"):
        # SQLite: NullPool — планировщик воркера пишет из нескольких потоков,
        # соединения не держим в пуле (WAL обеспечивает конкурентность чтения).
        kwargs.update(
            connect_args={"check_same_thread": False},
            poolclass=pool.NullPool,
        )
    else:
        kwargs.update(
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle,
        )

    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - обёртка
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
            # Unicode-aware lower для LIKE (встроенный lower() в SQLite — только
            # ASCII; кириллица не приводится). См. filter_compile._like_ci.
            dbapi_conn.create_function(
                "u_lower", 1, lambda s: s.lower() if isinstance(s, str) else s,
                deterministic=True,
            )

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    """Engine процесса; создаётся лениво и переиспользуется."""
    global _engine
    if _engine is None:
        _engine = _build_engine(settings or get_settings())
        logger.info("DB engine создан: %s", _masked_url())
    return _engine


def _masked_url() -> str:
    """URL без пароля — для логов."""
    eng = _engine
    return repr(eng.url) if eng is not None else "<no engine>"


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), expire_on_commit=False, future=True
        )
    return _session_factory


# Короткое имя для кода, которому нужен «просто новый Session».
SessionLocal = get_session_factory


@contextmanager
def session_scope(*, commit: bool = True) -> Iterator[Session]:
    """Транзакционная граница: commit при успехе, rollback при исключении.

    Используется воркером (stage → upsert) и админкой; HTTP-слой Flask
    получит свой скоуп через before/teardown на шаге 5.
    """
    session = get_session_factory()()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(settings: Settings | None = None) -> None:
    """Создать служебные таблицы, если их нет (для разработки/тестов).

    В production схема версионируется Alembic (шаг 4); этот хелпер не делает
    ALTER существующих таблиц и поэтому безопасен как «bootstrap» тестовой БД.
    Динамические таблицы данных сюда не входят — только Base.metadata.
    """
    import odata_cache.models  # noqa: F401  — реестр моделей должен быть импортирован

    Base.metadata.create_all(get_engine(settings))


def dispose_engine() -> None:
    """Закрыть пул соединений (тесты, корректная остановка процесса)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
