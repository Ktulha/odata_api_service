"""Шаг 6.5: подмена пустых таблиц-прокладок (ручные правила + эвристика)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from odata_cache.db.base import Base
from odata_cache.metadata import parse_metadata
from odata_cache.models.registry import Entity
from odata_cache.services import access, entity_resolution, registry_service

SAMPLE = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0">
<Schema Namespace="StandardODATA">
<EntityType Name="Catalog_Номенклатура">
<Key><PropertyRef Name="Key"/></Key>
<Property Name="Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
<Property Name="ЕдиницаИзмерения_Key" Type="Edm.Guid" Nullable="true"/>
<NavigationProperty Name="ЕдиницаИзмерения" Relationship="NS.R_Eд" FromRole="Begin" ToRole="End"/>
</EntityType>
<EntityType Name="Catalog_ЕдиницыИзмерения">
<Key><PropertyRef Name="Key"/></Key>
<Property Name="Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="Catalog_КлассификаторЕдиницИзмерения">
<Key><PropertyRef Name="Key"/></Key>
<Property Name="Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="НаименованиеПолное" Type="Edm.String" Nullable="true"/>
</EntityType>
<Association Name="R_Eд">
<End Type="StandardODATA.Catalog_Номенклатура" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_ЕдиницыИзмерения" Role="End" Multiplicity="0..1"/>
</Association>
<EntityContainer Name="c">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
<EntitySet Name="Catalog_ЕдиницыИзмерения" EntityType="StandardODATA.Catalog_ЕдиницыИзмерения"/>
<EntitySet Name="Catalog_КлассификаторЕдиницИзмерения" EntityType="StandardODATA.Catalog_КлассификаторЕдиницИзмерения"/>
</EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>'''


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        registry_service.sync_metadata_to_registry(s, parse_metadata(SAMPLE))
        s.commit()
        yield s


def _nom_prop(session, name="ЕдиницаИзмерения_Key"):
    nom = access.get_entity(session, "Catalog_Номенклатура")
    prop = next(p for p in nom.properties if p.name == name)
    return nom, prop


def test_direct_target_without_alias(session):
    """Без настройки ссылка ведёт на прокладку — как в $metadata."""
    nom, prop = _nom_prop(session)
    r = entity_resolution.resolve_ref_target(session, nom, prop, prop.ref_entity_id)
    assert r is not None and r.via_alias is False and r.rule == "direct"
    assert session.get(Entity, r.effective_id).name == "Catalog_ЕдиницыИзмерения"


def _activate(session, *names):
    """Включить сущности глобально (админ-действие) — иначе alias не применяется:
    правило ТЗ «запрещено → не грузим» сильнее настройки подмены."""
    for n in names:
        access.get_entity(session, n).is_active = True


def test_global_alias_redirects(session):
    """Глобальная прокладка: ЕдиницыИзмерения → КлассификаторЕдиницИзмерения."""
    _activate(session, "Catalog_Номенклатура", "Catalog_ЕдиницыИзмерения",
              "Catalog_КлассификаторЕдиницИзмерения")
    entity_resolution.set_alias(
        session,
        source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения",
        note="таблица пустая, данные в классификаторе",
    )
    session.commit()
    nom, prop = _nom_prop(session)
    r = entity_resolution.resolve_ref_target(session, nom, prop, prop.ref_entity_id)
    assert r.via_alias is True and r.rule == "alias_global"
    assert session.get(Entity, r.effective_id).name == "Catalog_КлассификаторЕдиницИзмерения"


def test_disabled_target_alias_ignored(session):
    """Запрещённая цель-подмена не применяется: правило ТЗ сильнее настройки."""
    entity_resolution.set_alias(
        session, source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения",
    )
    tgt = access.get_entity(session, "Catalog_КлассификаторЕдиницИзмерения")
    tgt.is_active = False          # глобально выключена
    session.commit()
    nom, prop = _nom_prop(session)
    r = entity_resolution.resolve_ref_target(session, nom, prop, prop.ref_entity_id)
    assert r.via_alias is False and r.effective_id == prop.ref_entity_id


def test_link_override_beats_alias(session):
    """link_override владельца конкретнее alias-правила."""
    _activate(session, "Catalog_Номенклатура", "Catalog_ЕдиницыИзмерения",
              "Catalog_КлассификаторЕдиницИзмерения")
    entity_resolution.set_alias(
        session, source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения",
    )
    nom = access.get_entity(session, "Catalog_Номенклатура")
    # подмена ведёт на другую РЕАЛЬНУЮ сущность, отличную от цели по умолчанию
    nom.link_override = {"ЕдиницаИзмерения_Key": "Catalog_КлассификаторЕдиницИзмерения"}
    session.commit()
    _, prop = _nom_prop(session)
    r = entity_resolution.resolve_ref_target(session, nom, prop, prop.ref_entity_id)
    assert r.rule == "override"
    assert session.get(Entity, r.effective_id).name == "Catalog_КлассификаторЕдиницИзмерения"


def test_owner_property_alias_specificity(session):
    """Точечное правило (owner+property) важнее глобальной прокладки."""
    _activate(session, "Catalog_Номенклатура", "Catalog_ЕдиницыИзмерения",
              "Catalog_КлассификаторЕдиницИзмерения")
    nom = access.get_entity(session, "Catalog_Номенклатура")
    entity_resolution.set_alias(
        session, source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения",
    )
    entity_resolution.set_alias(
        session, source="Catalog_ЕдиницыИзмерения",
        target="Catalog_Номенклатура",
        owner="Catalog_Номенклатура", property_name="ЕдиницаИзмерения_Key",
    )
    session.commit()
    _, prop = _nom_prop(session)
    r = entity_resolution.resolve_ref_target(session, nom, prop, prop.ref_entity_id)
    assert r.rule == "alias_prop"
    assert session.get(Entity, r.effective_id).name == "Catalog_Номенклатура"
    del nom


def test_suggest_aliases_finds_classifier(session):
    """Эвристика: для пустой прокладки предлагаем кандидата по суффиксу имени."""
    nom = access.get_entity(session, "Catalog_Номенклатура")
    nom.is_active = True
    eiu = access.get_entity(session, "Catalog_ЕдиницыИзмерения")
    clf = access.get_entity(session, "Catalog_КлассификаторЕдиницИзмерения")
    eiu.is_active = True
    clf.is_active = True
    session.commit()

    sugg = entity_resolution.suggest_aliases(session, ["Catalog_ЕдиницыИзмерения"])
    pairs = {(s.entity, s.candidate) for s in sugg}
    assert ("Catalog_ЕдиницыИзмерения", "Catalog_КлассификаторЕдиницИзмерения") in pairs
    # предложенный кандидат — самый вероятный (0.9: имя оканчивается на базу)
    best = max(sugg, key=lambda s: s.confidence)
    assert best.candidate == "Catalog_КлассификаторЕдиницИзмерения"
    # эвристика ничего не включает сама: правила в БД не появились
    assert entity_resolution.list_aliases(session) == []


def test_set_alias_validation(session):
    with pytest.raises(ValueError):
        entity_resolution.set_alias(session, source="Catalog_ЕдиницыИзмерения",
                                     target="Catalog_НетуТакой")
    with pytest.raises(ValueError):
        entity_resolution.set_alias(session, source="Catalog_ЕдиницыИзмерения",
                                    target="Catalog_ЕдиницыИзмерения")
    with pytest.raises(ValueError):
        entity_resolution.set_alias(session, source="Catalog_ЕдиницыИзмерения",
                                    target="Catalog_Номенклатура",
                                    property_name="ЕдиницаИзмерения_Key")  # без owner
    # цикл A→B→A по глобальным правилам
    entity_resolution.set_alias(session, source="Catalog_ЕдиницыИзмерения",
                                 target="Catalog_Номенклатура")
    session.commit()
    with pytest.raises(ValueError):
        entity_resolution.set_alias(session, source="Catalog_Номенклатура",
                                    target="Catalog_ЕдиницыИзмерения")


def test_list_and_remove_alias(session):
    a = entity_resolution.set_alias(
        session, source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения", note="тест",
    )
    session.commit()
    rows = entity_resolution.list_aliases(session)
    assert rows and rows[0]["source"] == "Catalog_ЕдиницыИзмерения"
    assert rows[0]["target"] == "Catalog_КлассификаторЕдиницИзмерения"
    assert entity_resolution.remove_alias(session, a.id) is True
    assert entity_resolution.list_aliases(session) == []
