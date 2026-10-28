"""DDL-слой: боевые и staging-таблицы сущностей (шаг 8).

Таблица данных = плоская копия полей 1С «как есть» (кириллица в именах
колонок допустима — SQLAlchemy котирует идентификаторы сам, PG и SQLite
принимают их). Правила отображения свойств (entity_properties):

- скаляр Edm.*            → колонка того же имени, тип из value_type;
- ссылка (*_Key + ref)    → GUID-колонка (FK смысловой, без DDL-ограничения:
                            цели докачиваются асинхронно, NULL-ключи 1С
                            нормализуются в NULL);
- *_Type у ссылки         → строковая колонка рядом (тип динамической ссылки);
- коллекция (RecordSet /  ДополнительныеРеквизиты) → отдельная дочерняя
  таблица data_<child>, owner_key → PK родителя (решение по образцу 1С);
- служебные DataVersion, odata.* — не храним.

``staging``-таблица — точная копия боевой: воркер грузит пакет в staging,
затем делает upsert в боевую (требование пользователя).

Идемпотентность: create_if_missing сравнивает набор колонок с реестром;
несовпадение (схема 1С поехала) → пересоздание staging и ALTER ADD COLUMN
для новых полев боевых (данные старых колонок сохраняются).
"""

from __future__ import annotations

import logging

from sqlalchemy import Column, MetaData, Table, Text, inspect, types
from sqlalchemy.engine import Engine

from odata_cache.models.registry import Entity, EntityProperty

logger = logging.getLogger(__name__)

# value_type из реестра → SQL-тип (одинаковый SQL генерируют PG и SQLite)
_TYPE_MAP: dict[str, type] = {
    "string": Text,
    "guid": Text,      # канонический lowercase-guid строкой; совместимо с SQLite
    "int": types.Integer,
    "float": types.Float,
    "decimal": Text,  # Edm.Decimal: храним строкой до появления реальных данных
    "bool": types.Boolean,
    "date": types.Date,
    "datetime": types.DateTime,
}

# поля 1С, которые никогда не кладём в таблицы
SKIP_PROPS = {"DataVersion"}


def _column_for(p: EntityProperty) -> Column:
    sqltype = _TYPE_MAP.get(p.value_type, Text)
    if p.value_type == "decimal" and p.precision:
        sqltype = types.Numeric(p.precision, p.scale or 0)
    nullable = True
    if p.is_key:
        nullable = False
    return Column(p.column_name, sqltype, nullable=nullable, primary_key=p.is_key)


def table_columns(props: list[EntityProperty]) -> list[Column]:
    """Колонки таблицы по свойствам реестра (PK — составной, как Key в $metadata)."""
    cols = [_column_for(p) for p in props if p.name not in SKIP_PROPS]
    if not any(c.primary_key for c in cols):
        raise ValueError("нет ключевых колонок — таблица не создаём")
    return cols


def ensure_table(
    engine: Engine,
    entity: Entity,
    props: list[EntityProperty],
    *,
    table_name: str | None = None,
) -> Table:
    """Создать/проверить таблицу; вернуть sqlalchemy.Table (без ORM)."""
    name = table_name or entity.storage_table
    meta = MetaData()
    table = Table(name, meta, *table_columns(props), extend_existing=True)
    existing_cols = set(inspect(engine).get_columns(name)) if inspect(engine).has_table(name) else set()
    want_cols = {c.name for c in table.columns}

    if not existing_cols:
        table.create(bind=engine, checkfirst=True)
        logger.info("создана таблица %s (%d колонок)", name, len(want_cols))
    elif missing := want_cols - existing_cols:
        # схема 1С расширилась — добавляем колонки (NULL-допустимые кроме PK)
        from sqlalchemy.schema import AddColumn  # noqa: F401  (documented intent)
        with engine.begin() as conn:
            for col in missing:
                c = table.columns[col]
                conn.exec_driver_sql(
                    f'ALTER TABLE "{name}" ADD COLUMN "{col}" {c.type.compile(conn.engine.dialect)}'
                )
        logger.info("таблица %s: добавлены колонки %s", name, sorted(missing))
    return table


def ensure_staging(engine: Engine, entity: Entity, props: list[EntityProperty]) -> Table:
    """Staging = копия боевой без PK-ограничений; пересоздаётся при смене схемы."""
    name = entity.staging_table
    cols = []
    for p in props:
        if p.name in SKIP_PROPS:
            continue
        c = _column_for(p)
        c.primary_key = False
        cols.append(Column(p.column_name, _TYPE_MAP.get(p.value_type, Text), nullable=True))
    meta = MetaData()
    table = Table(name, meta, *cols)
    insp = inspect(engine)
    if insp.has_table(name):
        have = {c["name"] for c in insp.get_columns(name)}
        if have != {c.name for c in cols}:
            table.drop(bind=engine)
            table.create(bind=engine)
            logger.info("staging %s пересоздан (смена схемы)", name)
    else:
        table.create(bind=engine)
        logger.info("создан staging %s", name)
    return table


def child_tables_for(session_props: list[EntityProperty]) -> list[tuple[str, int]]:
    """Дочерние коллекции сущности: [(ref_entity_id, ...)] — id целевых Entity."""
    return [p.ref_entity_id for p in session_props if p.ref_kind == "collection" and p.ref_entity_id]
