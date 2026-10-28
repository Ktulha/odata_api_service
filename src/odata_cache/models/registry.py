"""Реестр сущностей 1С и их свойств (шаг 3).

Здесь живёт «белый список»: API и воркер знают о сущностях РОВНО столько,
сколько записано в ``entities``/``entity_properties``. Таблица свойств — это
кеш разобранного ``$metadata`` (пункт Q3): worker один раз скачал метаданные,
разложил в ``odata_metadata_raw`` и спарсил в свойства; дальше и боевые DDL,
и сериализация ответов собираются из ``entity_properties`` без повторного
парсинга.

Ключевые правила, завязанные на эти таблицы:
- ``is_active=False`` → сущность не синхронизируется и не отдаётся API
  (данные удаляются из БД вместе со ссылками на неё);
- ``cacheable=False`` → техническая сущность, никогда не кешируется;
- ``storage_table``/``staging_table`` связывают запись реестра с реальной
  динамической таблицей данных (создаётся воркером, шаг 8);
- ссылки (NavigationProperty) хранятся как ``ref_kind='ref'`` +
  ``ref_entity_id`` → в боевой таблице это колонка ``<Prop>_Key INTEGER
  REFERENCES <target storage_table>`` (Номенклатура_Key → ref.key).
"""

from __future__ import annotations

import re

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from odata_cache.db.base import Base
from odata_cache.models.common import TimestampMixin

# Имена таблиц данных выводим из имени сущности. 1С отдаёт EntitySet с
# кириллицей («AccumulationRegister_ЗапасыПереданныеВРемонте»), поэтому
# разрешаем Unicode-буквы: и PG, и SQLite принимают их в двойных кавычках
# (SQLAlchemy идентификаторы котирует сам). Дефис/пробел — нельзя.
_IDENT_RE = re.compile(r"^[^\W\d][\w]*$", re.UNICODE)


def _assert_ident(ident: str, entity_name: str) -> str:
    if not _IDENT_RE.match(ident):
        raise ValueError(f"некорректное имя сущности для таблицы: {entity_name!r}")
    if len(ident.encode("utf-8")) > 63:
        # PostgreSQL молча обрезает идентификаторы до 63 байт — режем осознанно
        # с детерминированным суффиксом по хэшу, чтобы имена не сталкивались.
        import hashlib

        digest = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:8]
        ident = ident.encode("utf-8")[:54].decode("utf-8", "ignore") + "_" + digest
    return ident


def storage_table_for(entity_name: str) -> str:
    """«Боевая» таблица сущности: data_catalog_номенклатура."""
    return _assert_ident(f"data_{entity_name.strip().lower()}", entity_name)


def staging_table_for(entity_name: str) -> str:
    """Временная таблица загрузки: stage_... (пересоздаётся каждую загрузку)."""
    return _assert_ident(f"stage_{entity_name.strip().lower()}", entity_name)


class Entity(TimestampMixin, Base):
    """Сущность OData из реестра (единажды заведённая админом/воркером)."""

    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # имя как в URL OData (напр. "Номенклатура" или "Nomenclature") —
    # canonical-ключ реестра; регистр сохраняем, уникальность — без учёта регистра
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # полное имя EntityType из $metadata (Ns.Nomenclature) — для сопоставления при sync
    odata_type_name: Mapped[str | None] = mapped_column(String(256))
    entity_set_name: Mapped[str | None] = mapped_column(String(128))

    title_property: Mapped[str | None] = mapped_column(String(128))  # напр. "Description"
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    cacheable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # ключ записи в 1С: обычно "key"/"Ref_Key"; значение всегда приводим к строке
    key_property: Mapped[str] = mapped_column(String(128), default="key", nullable=False)

    storage_table: Mapped[str] = mapped_column(String(128), nullable=False)
    staging_table: Mapped[str] = mapped_column(String(128), nullable=False)

    # жизненный цикл кеширования (см. services/registry_service.STATE_*):
    # discovered → nothing грузим; scheduled/on_demand → можно грузить; disabled → запрещено
    state: Mapped[str] = mapped_column(String(16), default="discovered", nullable=False)

    # статистика последней успешной синхронизации (для админки/отладки)
    last_synced_at: Mapped[str | None] = mapped_column(Text)
    last_sync_count: Mapped[int | None] = mapped_column(Integer)
    schema_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    properties: Mapped[list[EntityProperty]] = relationship(
        back_populates="entity",
        cascade="all, delete-orphan",
        order_by="EntityProperty.position",
        # у entity_properties два FK на entities (owner + ref target) —
        # явно указываем, что «владелец» это entity_id
        foreign_keys="EntityProperty.entity_id",
    )

    def __repr__(self) -> str:  # pragma: no cover - отладочный хелпер
        return f"<Entity {self.name} active={self.is_active}>"


class EntityProperty(Base):
    """Свойство сущности (структурированный кеш $metadata)."""

    __tablename__ = "entity_properties"
    __table_args__ = (UniqueConstraint("entity_id", "name", name="uq_entity_property"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)   # как в 1С
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)  # в нашей таблице

    # логический тип: Edm.* из metadata либо наш "ref" (ссылка на справочник)
    edm_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # тип колонки в БД: string|int|float|decimal|bool|date|datetime|guid
    value_type: Mapped[str] = mapped_column(String(16), nullable=False)

    nullable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    max_length: Mapped[int | None] = mapped_column(Integer)
    precision: Mapped[int | None] = mapped_column(Integer)
    scale: Mapped[int | None] = mapped_column(Integer)

    is_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_title: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # свойство доступно API (включая expand); False → скрыто даже у разрешённой сущности
    exposed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ссылка: target-сущность реестра; ON DELETE SET NULL → при запрете target
    # ссылка обнуляется и в вывод не попадает (см. правило Q3/принцип 3 плана)
    ref_kind: Mapped[str | None] = mapped_column(String(16))  # 'ref' | 'collection' | None
    ref_entity_id: Mapped[int | None] = mapped_column(
        ForeignKey("entities.id", ondelete="SET NULL")
    )

    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    entity: Mapped[Entity] = relationship(back_populates="properties",
                                          foreign_keys=[entity_id])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<EntityProperty {self.entity_id}.{self.name}:{self.value_type}>"
