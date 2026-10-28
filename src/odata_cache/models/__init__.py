"""Реестр моделей пакета (шаг 3).

Импорт этого модуля гарантирует, что все таблицы зарегистрированы в
``Base.metadata`` — это нужно для Alembic-автогенерации и ``init_db()``.
"""

from odata_cache.models.auth import (
    ApiToken,
    Role,
    RoleEntity,
    generate_secret,
    hash_token,
)
from odata_cache.models.common import TimestampMixin, UTCText, utcnow_iso
from odata_cache.models.metadata import MetadataSnapshot, ODataMetadata
from odata_cache.models.registry import (
    Entity,
    EntityProperty,
    staging_table_for,
    storage_table_for,
)
from odata_cache.models.sync import RequestLog, SyncJob, SyncRun

__all__ = [
    "ApiToken",
    "Entity",
    "EntityProperty",
    "MetadataSnapshot",
    "ODataMetadata",
    "RequestLog",
    "Role",
    "RoleEntity",
    "SyncJob",
    "SyncRun",
    "TimestampMixin",
    "UTCText",
    "generate_secret",
    "hash_token",
    "staging_table_for",
    "storage_table_for",
    "utcnow_iso",
]
