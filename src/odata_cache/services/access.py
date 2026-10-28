"""Политика доступа и жизненный цикл кеширования (шаг 4, продолжение).

Здесь собраны **единственные** точки, через которые остальные слои узнают про
разрешения. Никакого «самостоятельного» чтения таблиц entities/role_entities в
API/worker быть не должно — иначе легко разойтись с правилами ТЗ.

Правила:

- ``is_active`` — глобальный выключатель. False ⇒ сущность недоступна всем,
  не кешируется, ссылки на неё исчезают из вывода;
- ``cacheable`` — технический признак (служебные типы 1С); False ⇒ то же самое,
  но без права включить админом;
- grant роли (``role_entities.can_read``) — локальный выключатель: даже активная
  сущность недоступна роли без grant;
- linked-entity правило: если у разрешённой сущности есть ссылка на ЗАПРЕЩЁННУЮ,
  запрещённая НЕ подтягивается в кеш и НЕ попадает в expand (колонка-FK просто
  остаётся Guid'ом без JOIN).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from odata_cache.models.auth import RoleEntity
from odata_cache.models.registry import Entity
from odata_cache.services.registry_service import (
    STATE_DISABLED,
    STATE_DISCOVERED,
    STATE_ON_DEMAND,
    STATE_SCHEDULED,
)

logger = logging.getLogger(__name__)


class AccessError(Exception):
    """Отказ в доступе. HTTP-слой превращает в 403/404 (см. шаг 5)."""

    def __init__(self, reason: str, entity: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.entity = entity


def get_entity(session: Session, name: str) -> Entity | None:
    """Сущность реестра по имени (регистронезависимо: 1С отдаёт разный регистр)."""
    for ent in session.scalars(select(Entity)):
        if ent.name.lower() == name.lower():
            return ent
    return None


def is_globally_allowed(entity: Entity | None) -> bool:
    """Глобальный уровень: активна + кешируема."""
    return bool(entity and entity.is_active and entity.cacheable)


def allowed_entities(session: Session, role_id: int | None) -> set[str]:
    """Имена сущностей, доступные роли (с учётом глобального выключателя).

    Роль без grants не получает ничего — пустое множество, а не «всё».
    """
    active = {
        e.name
        for e in session.scalars(
            select(Entity).where(Entity.is_active.is_(True), Entity.cacheable.is_(True))
        )
    }
    if role_id is None:
        return set()
    granted = set(
        session.scalars(
            select(Entity.name)
            .join(RoleEntity, RoleEntity.entity_id == Entity.id)
            .where(RoleEntity.role_id == role_id, RoleEntity.can_read.is_(True))
        )
    )
    return active & granted


def require_entity_access(
    session: Session, *, name: str, role_id: int | None, admin: bool = False
) -> Entity:
    """Проверка доступа к сущности для data-API.

    Возвращает Entity, если доступна; иначе :class:`AccessError`.

    Различаем отказ так, чтобы не утекать структурой реестра:
    - сущности нет в реестре → ``unknown`` (404);
    - есть, но запрещена глобально → ``forbidden`` (403) — скрытие факта
      существования могло бы подсказать клиенту, что «что-то есть»; на практике
      для внешнего клиента оба варианта лучше сводить к 404 (настраивается на шаге 5);
    - разрешена глобально, но нет grant роли → ``not_granted`` (403).
    """
    entity = get_entity(session, name)
    if entity is None:
        raise AccessError("unknown", name)
    if admin:
        return entity
    if not is_globally_allowed(entity):
        raise AccessError("forbidden", name)
    if entity.name not in allowed_entities(session, role_id):
        raise AccessError("not_granted", name)
    return entity


# ---------------------------------------------------------------------------
# включение/выключение кеширования
# ---------------------------------------------------------------------------

def enable_caching(
    session: Session, name: str, *, via_schedule: bool = True
) -> Entity:
    """Включить сущность в кеш (делает это только админ или worker по расписанию).

    ``via_schedule=True`` → state=scheduled (жестко добавлено в расписание);
    ``via_schedule=False`` → state=on_demand (первое обращение клиента).

    Таблица данных здесь НЕ создаётся — это делает DDL-слой на шаге 8 перед
    первой загрузкой. Здесь лишь поднимаем флаги, чтобы проверка доступа
    и планировщик увидели сущность.
    """
    entity = get_entity(session, name)
    if entity is None:
        raise AccessError("unknown", name)
    if not entity.cacheable:
        raise AccessError("not_cacheable", name)
    entity.is_active = True
    entity.state = STATE_SCHEDULED if via_schedule else STATE_ON_DEMAND
    logger.info("кеш включён: %s (%s)", entity.name, entity.state)
    return entity


def disable_caching(session: Session, name: str) -> Entity:
    """Выключить сущность: данные будут удалены, ссылки перестанут раскрываться.

    Сама операция удаления строк таблицы данных — на worker’е (шаг 8): тут мы
    меняем только состояние реестра, чтобы API мгновенно начал отвечать 403.
    """
    entity = get_entity(session, name)
    if entity is None:
        raise AccessError("unknown", name)
    entity.is_active = False
    entity.state = STATE_DISABLED
    logger.info("кеш выключен: %s — данные и ссылки будут убраны", entity.name)
    return entity


def needs_initial_load(entity: Entity) -> bool:
    """Признак «при первом обращении надо скачать из 1С»: таблица ещё не заполнялась."""
    return entity.last_synced_at is None and entity.state in (
        STATE_ON_DEMAND,
        STATE_SCHEDULED,
    )


def cacheable_dependency_names(session: Session, entity: Entity) -> list[str]:
    """Имена сущностей, на которые ссылается ``entity`` и которые тоже надо закэшить.

    Правило ТЗ: при on-demand загрузке пишем «все разрешённые подчинённые
    таблицы». Подчинёнными считаем цели ссылок (ref_entity_id), которые сами
    активны и кешируемы; запрещённые цели сюда не попадают → они не грузятся и
    остаются простыми Guid-колонками.
    """
    names: list[str] = []
    seen: set[int] = {entity.id}
    queue: list[Entity] = [entity]
    while queue:
        cur = queue.pop(0)
        for prop in cur.properties:
            target_id = prop.ref_entity_id
            if target_id is None or target_id in seen:
                continue
            seen.add(target_id)
            target = session.get(Entity, target_id)
            if target is None or not is_globally_allowed(target):
                continue
            names.append(target.name)
            queue.append(target)
    return names
