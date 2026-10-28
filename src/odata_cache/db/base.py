"""Декларативная база SQLAlchemy (шаг 3).

``Base`` — общая для всех статических таблиц реестра/авторизации/журнала.
Динамические таблицы данных 1С (шаг 8) создаются в отдельном metadata,
чтобы ``create_all`` служебных таблиц случайно не тронул их.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Стандартные имена ограничений — нужно для Alembic (авто-генерация diff
# корректно работает только с именованными constraints).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """База для статических (проектируемых руками) таблиц."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Метаданные динамических таблиц данных (заполняются воркером на шаге 8).
dynamic_metadata = MetaData(naming_convention=NAMING_CONVENTION)
