"""Конфигурация сервиса (шаг 2).

Все настройки читаются из переменных окружения (префикс ``ODATA_``)
и/или файла ``.env`` в корне проекта. Значения по умолчанию рассчитаны
на локальную разработку с SQLite; для production — PostgreSQL.

Важно: реквизиты 1С (onedata_*) нужны ТОЛЬКО роли worker. Код data/admin API
эти поля не использует и не должен использовать.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ODATA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- приложение ---
    app_name: str = "ics-odata-api"
    debug: bool = False

    # --- база данных кеша (PostgreSQL или SQLite) ---
    database_url: str = "sqlite:///./odata_cache.db"
    sql_echo: bool = False          # логировать SQL (отладка)
    db_pool_size: int = 5           # только для PostgreSQL-URL
    db_max_overflow: int = 10
    db_pool_recycle: int = 1800

    # --- 1С OData (использует ТОЛЬКО worker) ---
    onedata_base_url: str = ""      # напр. https://host/OData/Publish.svc
    onedata_username: str = ""      # basic-auth для OData.publish
    onedata_password: str = ""
    onedata_timeout: float = 30.0   # секунд на HTTP-запрос к 1С

    # --- worker / планировщик ---
    sync_default_cron: str = "*/15 * * * *"  # расписание по умолчанию для новых сущностей
    scheduler_enabled: bool = True           # выключать на роли api и в тестах

    # --- pagination (data API) ---
    default_page_size: int = 50
    max_page_size: int = 1000


@lru_cache
def get_settings() -> Settings:
    """Единая точка доступа к настройкам (кэш на процесс)."""
    return Settings()
