"""Разрешение «настоящей» сущности для ссылок и пустых таблиц-прокладок (шаг 6.5).

Проблема (из ТЗ): ``Catalog_ЕдиницыИзмерения`` в OData есть, но таблица пустая —
реальные наименования единиц измерения лежат в ``Catalog_КлассификаторЕдиницИзмерения``.
Такое возможно и для других сущностей. Решение — «а+б»:

**а) ручная настройка в БД** (:class:`~odata_cache.models.registry.EntityAlias` +
``Entity.link_override``): админ явно говорит, что прокладка → куда смотреть, или
что ссылка конкретного свойства конкретной сущности ведёт в другое место;

**б) эвристика**: при синхронизации видим, что разрешённая к загрузке сущность
отдаёт 0 строк, а на неё есть ссылки — предлагаем кандидата-заменителя по имени
(суффикс после префикса области, например ``ЕдиницыИзмерения`` →
``КлассификаторЕдиницИзмерения``/``…ЕдиницИзмерения``). Эвристика НИЧЕГО сама не
включает: она возвращает список :class:`AliasSuggestion`, который админка показывает
на подтверждение.

Почему разрешение живёт отдельным модулем, а не внутри load_plan/access: правило
подмены нужно сразу трём слоям — план загрузки (какой EntitySet качаем), доступ
(права проверяем по реальной сущности) и API-сериализация (expand идёт в реальную
таблицу). Одна точка правды = нет расхождений.

Приоритет правил (от частного к общему):
1. ``link_override`` владельца (prop → target);
2. точечный alias (owner + property_name + source);
3. alias без property (owner + source);
4. глобальный alias (source → target, «прокладка»);
5. исходная цель из реестра (без подмены).

Запрещённая цель-подмена НЕ применяется (правило ТЗ сильнее настройки): если
target выключен/некешируем, остаётся исходное поведение — ссылку не грузим.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from odata_cache.models.registry import Entity, EntityAlias, EntityProperty
from odata_cache.services.access import get_entity, is_globally_allowed
from odata_cache.services.registry_service import _SET_PREFIXES, ref_property_base

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ResolvedRef:
    """Итог разрешения одной ссылки: чем грузим и чьи права проверяем."""

    source_id: int            # сущность, на которую формально указывает свойство
    effective_id: int         # реальная сущность для загрузки/чтения
    via_alias: bool           # сработала ли подмена
    rule: str                 # 'override' | 'alias_prop' | 'alias_owner' | 'alias_global' | 'direct'

    @property
    def changed(self) -> bool:
        return self.effective_id != self.source_id


def resolve_ref_target(
    session: Session,
    owner: Entity,
    prop: EntityProperty,
    source_id: int | None,
) -> ResolvedRef | None:
    """Вернуть эффективную цель ссылки ``owner.prop`` (или None, если цели нет)."""
    if source_id is None:
        return None

    # 1) link_override владельца: {"ЕдиницаИзмерения_Key": "Catalog_Класс…"}
    override_name = owner.link_override_map.get(prop.name)
    if override_name:
        ent = get_entity(session, override_name)
        if ent is not None and is_globally_allowed(ent):
            return ResolvedRef(source_id, ent.id, True, "override")
        logger.warning(
            "link_override %s.%s → %s проигнорирован: цель недоступна",
            owner.name, prop.name, override_name,
        )

    # 2..4) таблица aliases: от точечного правила к глобальному
    rows = session.scalars(
        select(EntityAlias).where(
            EntityAlias.entity_id == source_id,
            EntityAlias.enabled.is_(True),
        )
    ).all()
    for a in sorted(rows, key=lambda x: -_specificity(x, owner, prop)):
        target = session.get(Entity, a.target_id)
        if target is None or not is_globally_allowed(target):
            continue
        kind = {
            3: "alias_prop",
            2: "alias_owner",
            1: "alias_global",
        }[_specificity(a, owner, prop)]
        return ResolvedRef(source_id, target.id, True, kind)

    return ResolvedRef(source_id, source_id, False, "direct")


def _specificity(alias: EntityAlias, owner: Entity, prop: EntityProperty) -> int:
    """3 = owner+property, 2 = owner (любое свойство), 1 = глобальная прокладка."""
    if alias.owner_id == owner.id and alias.property_name == prop.name:
        return 3
    if alias.owner_id == owner.id and alias.property_name is None:
        return 2
    if alias.owner_id is None and alias.property_name is None:
        return 1
    if alias.owner_id is None and alias.property_name == prop.name:
        return 2  # имя свойства без владельца — чуть менее конкретно, чем owner+prop
    return 0


def resolve_for_plan(session: Session, entity: Entity) -> dict[str, ResolvedRef]:
    """Все ссылки сущности разом (для build_load_plan / expand в API)."""
    out: dict[str, ResolvedRef] = {}
    for prop in entity.properties:
        if not prop.ref_kind or prop.ref_entity_id is None:
            continue
        r = resolve_ref_target(session, entity, prop, prop.ref_entity_id)
        if r is not None:
            out[prop.name] = r
    return out


# ---------------------------------------------------------------------------
# эвристика «пустая таблица → кандидат-заменитель» (пункт «б»)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class AliasSuggestion:
    """Предложение админу: прокладка ``entity`` похожа на заполненную ``candidate``."""

    entity: str
    candidate: str
    reason: str
    confidence: float


def suggest_aliases(session: Session, empty_names: list[str]) -> list[AliasSuggestion]:
    """Для списка пустых сущностей подобрать кандидатов из реестра.

    Никуда не ходим и ничего не включаем: только читаем реестр. Кандидат обязан
    быть кешируемым и активным — иначе предлагать его бессмысленно.
    """
    suggestions: list[AliasSuggestion] = []
    all_entities = list(session.scalars(select(Entity)))
    by_name = {e.name: e for e in all_entities}

    for name in empty_names:
        src = by_name.get(name) or get_entity(session, name)
        if src is None:
            continue
        base = _strip_set_prefix(src.name)
        candidates = _candidates_by_suffix(base, all_entities)
        # уже настроенные галстуки не предлагаём повторно
        configured = set(
            session.scalars(
                select(EntityAlias.target_id).where(EntityAlias.entity_id == src.id)
            )
        )
        for cand, score, reason in candidates:
            if cand.id in configured or cand.id == src.id:
                continue
            if not is_globally_allowed(cand):
                continue
            suggestions.append(
                AliasSuggestion(src.name, cand.name, reason, round(score, 2))
            )
    suggestions.sort(key=lambda s: -s.confidence)
    return suggestions


def _strip_set_prefix(name: str) -> str:
    for prefix in _SET_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _candidates_by_suffix(base: str, entities: list[Entity]) -> list[tuple[Entity, float, str]]:
    """Похожие имена: суффиксное совпадение («ЕдиницыИзмерения» ⊂ «КлассификаторЕдиницИзмерения»).

    Балл: 0.9 — имя кандидата заканчивается на базовое имя; 0.75 — основа
    кандидата (после усечения падежного окончания) заканчивается на основу
    базы — так ловится род. падеж мн. ч. в классификаторах 1С
    («единицы/единиц», «склады/складов»); 0.7 — база входит в имя целиком;
    0.5 — просто общий префикс.
    """
    out: list[tuple[Entity, float, str]] = []
    low = base.lower()
    stem = _truncate_case(low)
    for e in entities:
        other_full = _strip_set_prefix(e.name).lower()
        if other_full == low:
            continue
        if other_full.endswith(low):
            out.append((e, 0.9, f"имя '{e.name}' оканчивается на '{base}'"))
            continue
        # сравнение по основе: у обоих имён отрезаем окончание последнего слова
        # (измерения/измерений) — оно меняется в разных сущностях чаще всего.
        # Гласные «и/е» в чередующихся основах (единиц/единицы) не учитываем.
        other_stem = _truncate_case(other_full)
        if len(stem) >= 6 and _ends_with_stem(other_stem, stem):
            out.append((e, 0.75, f"основа '{stem}' совпадает с окончанием '{e.name}'"))
        elif low in other_full:
            out.append((e, 0.7, f"'{base}' входит в имя '{e.name}'"))
        elif _common_stem(low, other_full) >= 5:
            out.append((e, 0.5, f"общий корень со '{e.name}'"))
    return out


def _ends_with_stem(whole: str, stem: str) -> bool:
    """Оканчивается ли ``whole`` на ``stem`` с нестрогим совпадением гласных.

    Сравнение идем СПРАВА (суффикс важнее): окончание слова в 1С меняется
    («единицы»/«единиц»), поэтому ищем максимальный общий суффикс и считаем
    совпадение по согласным — гласные пропускаем, чередующиеся «ы/и/е» тоже.
    Если длина совпавшей части >= len(stem) - 2 — считаем, что основа та же.
    """
    if whole.endswith(stem):
        return True
    # сравним справа: последние N символов должны совпасть по согласным
    def consonants(s: str) -> list[str]:
        return [c for c in s if c not in "аеоыяиюёуэьъ"]
    ws, ss = consonants(whole[-len(stem) - 3:]), consonants(stem)
    if len(ss) < 4 or len(ws) < len(ss):
        return False
    tail = ws[-len(ss):]
    return all(a == b for a, b in zip(tail, ss))


def _truncate_case(name: str) -> str:
    """Усечь падежное окончание последнего слова: «…измерения» → «…измерен».

    Грубая замена лемматизации: режем хвост из <гласная><согласная+> после
    последней гласной не ближе 3 символов к концу — для имён 1С этого хватает,
    чтобы «единицыизмерения» и «единицизмерения» дали общую основу.
    """
    trimmed = name.rstrip("аеоыяиюёуь")
    for i in range(len(trimmed) - 1, max(len(trimmed) - 4, 1), -1):
        if trimmed[i] in "аеоыяиюёу":
            return trimmed[:i]
    return trimmed


def _common_stem(a: str, b: str) -> int:
    """Длина общего préfixa в символах — грубая замена лемматизации."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


# ---------------------------------------------------------------------------
# CRUD для админки (тонкая обёртка: валидация + защита от циклов)
# ---------------------------------------------------------------------------

def set_alias(
    session: Session,
    *,
    source: str,
    target: str,
    owner: str | None = None,
    property_name: str | None = None,
    note: str | None = None,
) -> EntityAlias:
    """Создать/обновить правило подмены (админка).

    Проверяем: обе сущности существуют, target кешируем, цикл прямых подмен запрещён.
    """
    src = get_entity(session, source)
    tgt = get_entity(session, target)
    if src is None:
        raise ValueError(f"исходная сущность {source!r} не найдена")
    if tgt is None:
        raise ValueError(f"целевая сущность {target!r} не найдена")
    if not tgt.cacheable:
        raise ValueError(f"цель {target!r} нельзя кешировать (cacheable=False)")
    if src.id == tgt.id:
        raise ValueError("подмена не может указывать на саму себя")

    owner_ent = get_entity(session, owner) if owner else None
    if owner and owner_ent is None:
        raise ValueError(f"владелец правила {owner!r} не найден")
    if property_name and owner_ent is None:
        raise ValueError("property_name требует указания owner")
    if property_name and owner_ent is not None:
        if not any(p.name == property_name for p in owner_ent.properties):
            raise ValueError(
                f"свойство {property_name!r} не найдено у {owner_ent.name!r}"
            )

    # защита от цикла A→B→A (по глобальным правилам)
    if owner_ent is None:
        chain, cur = {src.id}, tgt
        while cur is not None:
            nxt = session.scalar(
                select(EntityAlias).where(
                    EntityAlias.owner_id.is_(None),
                    EntityAlias.enabled.is_(True),
                    EntityAlias.entity_id == cur.id,
                )
            )
            if nxt is None:
                break
            cur = session.get(Entity, nxt.target_id)
            if cur is None or cur.id in chain:
                raise ValueError("циклическая цепочка подмен")
            chain.add(cur.id)

    alias = session.scalar(
        select(EntityAlias).where(
            EntityAlias.owner_id.is_(owner_ent.id if owner_ent else None),
            EntityAlias.property_name.is_(property_name),
            EntityAlias.entity_id == src.id,
        )
    )
    if alias is None:
        alias = EntityAlias(
            owner_id=owner_ent.id if owner_ent else None,
            property_name=property_name,
            entity_id=src.id,
            target_id=tgt.id,
            note=note,
        )
        session.add(alias)
    else:
        alias.target_id = tgt.id
        alias.enabled = True
        alias.note = note
    session.flush()
    logger.info("alias: %s → %s (%s)", src.name, tgt.name, property_name or "global")
    return alias


def remove_alias(session: Session, alias_id: int) -> bool:
    alias = session.get(EntityAlias, alias_id)
    if alias is None:
        return False
    session.delete(alias)
    session.flush()
    return True


def list_aliases(session: Session) -> list[dict]:
    """Правила для таблицы админки (имена вместо id — понятнее человеку)."""
    out: list[dict] = []
    for a in session.scalars(select(EntityAlias).order_by(EntityAlias.id)):
        owner = session.get(Entity, a.owner_id) if a.owner_id else None
        src = session.get(Entity, a.entity_id)
        tgt = session.get(Entity, a.target_id)
        out.append(
            {
                "id": a.id,
                "owner": owner.name if owner else None,
                "property": a.property_name,
                "source": src.name if src else None,
                "target": tgt.name if tgt else None,
                "enabled": a.enabled,
                "note": a.note,
            }
        )
    return out
