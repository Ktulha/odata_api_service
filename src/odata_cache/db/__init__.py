"""Слой работы с базой данных кеша (шаг 3).

Наружу экспортируются только стабильные вещи: ``Base``, ``get_engine``,
``SessionLocal``, ``init_db`` и декоратор ``session_scope``. Остальное —
внутренняя реализация модулей пакета.
"""

from odata_cache.db.base import Base
from odata_cache.db.engine import (
    SessionLocal,
    get_engine,
    init_db,
    session_scope,
)

__all__ = [
    "Base",
    "SessionLocal",
    "get_engine",
    "init_db",
    "session_scope",
]
