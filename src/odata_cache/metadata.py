"""Парсер CSDL ($metadata) 1С → плоская структура (шаг 3).

Разбираем XML через ElementTree, но namespace-агностично: теги ищутся по
локальному имени (``local-name()``), поэтому одинаково работают все версии
edmx/edm-пространств имён, которые отдаёт 1С.

Что извлекаем и зачем это нужно для «понятных таблиц»:

- ``EntityTypeInfo`` — свойства EntityType + ключ + NavigationProperty;
- тип строки данных определяется по EntitySet'у: ``EntitySet(name, entity_type)``;
- **ссылки**: свойство ``Организация_Key`` (Edm.Guid) связывается с
  NavigationProperty ``Организация`` → целевая сущность. Именно так получается
  связь ``Номенклатура_Key → ref.key`` (пункт Q3);
- регистры 1С: у основного EntityType есть ``RecordSet`` типа
  ``Collection(..._RowType/_RecordType)`` — это «таблица записей», её регистрируем
  как самостоятельную (дочернюю) сущность, потому что данные лежат именно там;
- наследование: поля ``BaseType`` подтягиваются в дочерний тип.

Результат парсинга — чистые dataclass'ы без зависимости от ORM: их раскладывает
в таблицы ``entities``/``entity_properties`` отдельный слой (``registry_service``,
шаг 4).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# Collection(Ns.Type) — свойство-«таблица» (RecordSet у регистров 1С).
_COLLECTION_RE = re.compile(r"^Collection\((?P<inner>.+)\)$")

# 1С называет дочерние типы строк именно так.
_CHILD_SUFFIXES = ("_RowType", "_RecordType")


@dataclass(slots=True)
class PropInfo:
    """Свойство типа: скаляр, коллекция или навигация (ссылка)."""

    name: str
    type_name: str                  # "Edm.String" / "Ns.Catalog_Номенклатура"
    nullable: bool = True
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    is_collection: bool = False
    is_navigation: bool = False
    # заполняется при разрешении связей:
    ref_target: str | None = None   # имя EntityType-цели (без namespace)
    ref_kind: str | None = None     # 'ref' | 'collection'
    # сырые атрибуты CSDL — нужны только на время разбора связей
    raw_relationship: str = ""
    raw_torole: str = "End"
    raw_partner: str = ""


@dataclass(slots=True)
class EntityTypeInfo:
    """EntityType из $metadata."""

    name: str                       # без namespace, напр. Catalog_Номенклатура
    namespace: str = ""
    open_type: bool = False
    base_type: str | None = None
    keys: list[str] = field(default_factory=list)
    properties: dict[str, PropInfo] = field(default_factory=dict)
    navigation: dict[str, PropInfo] = field(default_factory=dict)
    child_types: list[str] = field(default_factory=list)

    def add(self, prop: PropInfo) -> None:
        self.properties[prop.name] = prop

    @property
    def key_properties(self) -> list[PropInfo]:
        return [self.properties[k] for k in self.keys if k in self.properties]


@dataclass(slots=True)
class MetadataModel:
    """Всё, что знаем о схеме OData после разбора $metadata."""

    namespace: str = ""
    service_version: str | None = None
    entity_types: dict[str, EntityTypeInfo] = field(default_factory=dict)
    complex_types: dict[str, EntityTypeInfo] = field(default_factory=dict)
    entity_sets: dict[str, str] = field(default_factory=dict)      # Set → EntityType
    associations: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    def entity_type_for_set(self, set_name: str) -> EntityTypeInfo | None:
        et = self.entity_sets.get(set_name)
        return self.entity_types.get(et) if et else None

    @property
    def cacheable_entity_names(self) -> list[str]:
        """Имена EntitySet'ов, которые вообще можно кешировать («белый список» источников)."""
        return sorted(self.entity_sets)


def _local(tag: str) -> str:
    """'{http://...}EntityType' → 'EntityType'."""
    return tag.rsplit("}", 1)[-1]


def _short(qualified: str | None) -> str:
    """'StandardODATA.Catalog_Номенклатура' → 'Catalog_Номенклатура'."""
    if not qualified:
        return ""
    return qualified.rsplit(".", 1)[-1]


def _parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"true", "1"}


def _opt_int(value: str | None) -> int | None:
    return int(value) if value is not None and value.lstrip("-").isdigit() else None


def _make_property(elem: ET.Element) -> PropInfo:
    raw_type = elem.get("Type", "Edm.String")
    m = _COLLECTION_RE.match(raw_type)
    is_collection = bool(m)
    type_name = _short(m.group("inner")) if m else _short(raw_type)
    return PropInfo(
        name=elem.get("Name", ""),
        type_name=type_name,
        nullable=_parse_bool(elem.get("Nullable"), default=True),
        is_collection=is_collection,
        max_length=_opt_int(elem.get("MaxLength")),
        precision=_opt_int(elem.get("Precision")),
        scale=_opt_int(elem.get("Scale")),
    )


def _find_child(elem: ET.Element, local_name: str) -> ET.Element | None:
    """Первый прямой потомок с нужным локальным именем (ElementTree не умеет
    local-name() в XPath — поэтому ручной перебор)."""
    for child in elem:
        if _local(child.tag) == local_name:
            return child
    return None


def parse_metadata(xml_text: str) -> MetadataModel:
    """Разобрать строку $metadata в :class:`MetadataModel`.

    Схема: основной проход по Schema/EntityType/ComplexType/EntityContainer,
    затем второй проход — разрешение Association → целевые типы навигаций,
    затем постобработка (связки ``<Nav>_Key`` и наследование BaseType).
    """
    root = ET.fromstring(xml_text)
    model = MetadataModel()

    # --- версионирование сервиса (m:DataServiceVersion / m:MaxDataServiceVersion)
    for node in root.iter():
        if _local(node.tag) != "DataServices":
            continue
        for attr, val in node.attrib.items():
            if _local(attr) == "MaxDataServiceVersion":
                model.service_version = val
        if model.service_version is None:
            for attr, val in node.attrib.items():
                if _local(attr) == "DataServiceVersion":
                    model.service_version = val
        break

    # --- типы и контейнеры
    for schema in root.iter():
        if _local(schema.tag) != "Schema":
            continue
        ns = schema.get("Namespace") or ""
        if ns:
            model.namespace = _short(ns)

        for member in schema:
            tag = _local(member.tag)

            if tag in ("EntityType", "ComplexType"):
                info = EntityTypeInfo(
                    name=member.get("Name", ""),
                    namespace=model.namespace,
                    open_type=_parse_bool(member.get("OpenType")),
                    base_type=_short(member.get("BaseType")) or None,
                )
                if not info.name:
                    continue
                bucket = (
                    model.entity_types if tag == "EntityType" else model.complex_types
                )
                bucket[info.name] = info

                key_node = _find_child(member, "Key")
                if key_node is not None:
                    for pref in key_node:
                        if _local(pref.tag) == "PropertyRef":
                            nm = pref.get("Name", "")
                            if nm and nm not in info.keys:
                                info.keys.append(nm)

                for child in member:
                    ctag = _local(child.tag)
                    if ctag == "Property":
                        prop = _make_property(child)
                        if not prop.name:
                            continue
                        info.add(prop)
                        # RecordSet → дочерняя «таблица записей» регистра/накопителя
                        if prop.is_collection and prop.type_name and (
                            prop.type_name.endswith(_CHILD_SUFFIXES)
                            or prop.type_name in model.entity_types
                        ):
                            if prop.type_name not in info.child_types:
                                info.child_types.append(prop.type_name)
                    elif ctag == "NavigationProperty":
                        np = PropInfo(
                            name=child.get("Name", ""),
                            type_name="",
                            is_navigation=True,
                            nullable=True,
                        )
                        if np.name:
                            info.navigation[np.name] = np
                            info.properties.setdefault(np.name, np)
                        # связи разрешим вторым проходом — запоминаем сырые атрибуты
                        np.raw_relationship = _short(child.get("Relationship", ""))
                        np.raw_torole = child.get("ToRole", "End")
                        np.raw_partner = child.get("Partner", "")

            elif tag == "EntityContainer":
                for es in member:
                    if _local(es.tag) != "EntitySet":
                        continue
                    set_name = es.get("Name", "")
                    et = _short(es.get("EntityType", ""))
                    if set_name and et:
                        model.entity_sets[set_name] = et

    # --- Association'ы (могут объявляться позже NavigationProperty)
    for node in root.iter():
        if _local(node.tag) != "Association":
            continue
        assoc = _short(node.get("Name", ""))
        ends: dict[str, tuple[str, str]] = {}
        for end in node:
            if _local(end.tag) != "End":
                continue
            ends[end.get("Role", "")] = (
                _short(end.get("Type", "")),
                (end.get("Multiplicity", "1") or "1").strip(),
            )
        if assoc:
            model.associations[assoc] = ends

    _resolve_navigations(model)
    _link_key_properties(model)
    _inherit_base_types(model)
    _derive_child_owners(model)
    return model


def _resolve_navigations(model: MetadataModel) -> None:
    """Подставить в NavigationProperty целевой тип и кратность из Association."""
    for et in model.entity_types.values():
        for nav in et.navigation.values():
            rel, torole, partner = nav.raw_relationship, nav.raw_torole, nav.raw_partner
            ends = model.associations.get(rel)
            if ends is not None:
                end_type, mult = ends.get(torole, ("", ""))
            else:
                end_type, mult = "", ""
            if not end_type and partner:
                # V4-стиль: Partner указывает на обратную навигацию
                end_type = _guess_target_from_partner(model, et, nav, partner)
                mult = "*" if end_type and _is_child_table(end_type) else "1"
            if not end_type:
                # 1С без Association: цель выводим из имени навигации
                guessed = _guess_ref_target(model, nav.name)
                if guessed and guessed != et.name:
                    end_type, mult = guessed, "1"
            nav.type_name = end_type
            nav.ref_target = end_type or None
            nav.ref_kind = "collection" if mult.startswith("*") else "ref"


def _derive_child_owners(model: MetadataModel) -> None:
    """Добавить дочерним «таблицам» (RecordType/RowType) ссылку на родителя.

    В OData 1С данные регистра лежат в ``...RecordType``, а привязка к «шапке»
    только по смыслу имени типа. Превращаем это в явную навигацию ``Parent`` +
    скаляр ``Parent_Key`` — чтобы в БД была честная связь таблица→таблица.
    """
    owners = _child_owner_by_suffix(model)
    for child_name, parent_name in owners.items():
        child = model.entity_types.get(child_name)
        parent = model.entity_types.get(parent_name)
        if child is None or parent is None:
            continue
        key_props = parent.keys
        if len(key_props) != 1:
            # составной ключ «шапки» — явную FK не строим, связь остаётся логической
            continue
        pk = key_props[0]
        nav_name = "Parent"
        if nav_name not in child.navigation:
            nav = PropInfo(
                name=nav_name,
                type_name=parent.name,
                is_navigation=True,
                nullable=True,
                ref_target=parent.name,
                ref_kind="ref",
            )
            child.navigation[nav_name] = nav
            child.properties.setdefault(nav_name, nav)
        fk_name = f"{nav_name}_Key"
        if fk_name not in child.properties:
            child.add(
                PropInfo(
                    name=fk_name,
                    type_name=model.entity_types[parent_name].properties[pk].type_name,
                    nullable=True,
                    ref_target=parent.name,
                    ref_kind="ref",
                )
            )


def _guess_target_from_partner(
    model: MetadataModel, et: EntityTypeInfo, nav: PropInfo, partner: str
) -> str:
    """Если указан только Partner — найти тип, у которого есть навигация с таким именем."""
    for other in model.entity_types.values():
        if other.name == et.name:
            continue
        if partner in other.navigation:
            back = other.navigation[partner]
            if back.ref_target == et.name or not back.ref_target:
                return other.name
    return ""


def _child_owner_by_suffix(model: MetadataModel) -> dict[str, str]:
    """Дочерний тип таблицы (``Xxx_RowType``/``Xxx_RecordType``) → родительский тип.

    1С не всегда объявляет Association для RecordSet/Sections — тогда цель
    выводится из имени дочернего типа: ``Xxx_RowType`` → ``Xxx``; если такого
    EntityType нет, берём EntitySet с этим именем.
    """
    owners: dict[str, str] = {}
    for name in model.entity_types:
        for suffix in _CHILD_SUFFIXES:
            if name.endswith(suffix):
                parent = name[: -len(suffix)]
                if parent in model.entity_types:
                    owners[name] = parent
                elif parent in model.entity_sets:
                    owners[name] = model.entity_sets[parent]
                break
    return owners


def _guess_ref_target(model: MetadataModel, nav_name: str) -> str | None:
    """Эвристика для 1С: навигация ``Организация`` ↔ справочник ``Catalog_Организация``."""
    candidates = (f"Catalog_{nav_name}", f"ChartOfCharacteristicTypes_{nav_name}",
                  f"InformationRegister_{nav_name}", f"AccumulationRegister_{nav_name}",
                  f"Document_{nav_name}", nav_name)
    for cand in candidates:
        if cand in model.entity_types:
            return cand
    # совпадение по суффиксу имени (Организация vs …_Организация)
    for et_name in model.entity_types:
        if et_name.endswith(f"_{nav_name}") or et_name == nav_name:
            return et_name
    return None


def _is_child_table(type_name: str) -> bool:
    return type_name.endswith(_CHILD_SUFFIXES)


def _link_key_properties(model: MetadataModel) -> None:
    """Связать скаляр ``<Nav>_Key`` (Edm.Guid) с целевой сущностью навигации.

    Это ядро «понятных связей»: колонка ``Номенклатура_Key`` получает
    ``ref_target='Catalog_Номенклатура'`` и в боевой таблице станет FK.
    """
    for et in model.entity_types.values():
        for pname, prop in et.properties.items():
            if prop.is_navigation or prop.ref_kind:
                continue
            for nav_name, nav in et.navigation.items():
                if pname in (f"{nav_name}_Key", f"{nav_name}Key", f"{nav_name}_Ref_Key"):
                    prop.ref_target = nav.ref_target or nav.type_name or None
                    prop.ref_kind = nav.ref_kind or "ref"
                    break


def _inherit_base_types(model: MetadataModel) -> None:
    """Подтянуть свойства/ключи от BaseType (цикл защищён глубиной 8)."""
    for et in model.entity_types.values():
        base = et.base_type
        depth = 0
        while base and base in model.entity_types and depth < 8:
            parent = model.entity_types[base]
            for name, prop in parent.properties.items():
                et.properties.setdefault(name, prop)
            for k in parent.keys:
                if k not in et.keys:
                    et.keys.append(k)
            base = parent.base_type
            depth += 1
