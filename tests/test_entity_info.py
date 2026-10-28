"""Шаг 7.3: описание сущности (info) — поля, связи, статистика, фильтры."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from odata_cache.db.base import Base
from odata_cache.entity_info import build_catalog, cache_stats, entity_info
from odata_cache.metadata import parse_metadata
from odata_cache.services import access, entity_resolution, registry_service

SAMPLE = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0">
<Schema Namespace="StandardODATA">
<EntityType Name="Catalog_Номенклатура">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
<Property Name="Артикул" Type="Edm.String" Nullable="true"/>
<Property Name="Цена" Type="Edm.Double" Nullable="true"/>
<Property Name="ЭтоНабор" Type="Edm.Boolean" Nullable="true"/>
<Property Name="ДатаИзменения" Type="Edm.DateTime" Nullable="true"/>
<Property Name="ЕдиницаИзмерения_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="СкрытоеПоле" Type="Edm.String" Nullable="true"/>
<NavigationProperty Name="ЕдиницаИзмерения" Relationship="NS.R_Eд" FromRole="Begin" ToRole="End"/>
</EntityType>
<EntityType Name="Catalog_ЕдиницыИзмерения">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="Catalog_КлассификаторЕдиницИзмерения">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="НаименованиеПолное" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="AccumulationRegister_Запасы_RecordType">
<Key><PropertyRef Name="Recorder"/><PropertyRef Name="LineNumber"/></Key>
<Property Name="Recorder" Type="Edm.String" Nullable="false"/>
<Property Name="LineNumber" Type="Edm.Int64" Nullable="false"/>
<Property Name="Номенклатура_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Номенклатура_Type" Type="Edm.String" Nullable="true"/>
<Property Name="Количество" Type="Edm.Double" Nullable="true"/>
<NavigationProperty Name="Номенклатура" Relationship="NS.R_Nom" FromRole="Begin" ToRole="End"/>
</EntityType>
<Association Name="R_Eд">
<End Type="StandardODATA.Catalog_Номенклатура" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_ЕдиницыИзмерения" Role="End" Multiplicity="0..1"/>
</Association>
<Association Name="R_Nom">
<End Type="StandardODATA.AccumulationRegister_Запасы_RecordType" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_Номенклатура" Role="End" Multiplicity="0..1"/>
</Association>
<EntityContainer Name="c">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
<EntitySet Name="Catalog_ЕдиницыИзмерения" EntityType="StandardODATA.Catalog_ЕдиницыИзмерения"/>
<EntitySet Name="Catalog_КлассификаторЕдиницИзмерения" EntityType="StandardODATA.Catalog_КлассификаторЕдиницИзмерения"/>
<EntitySet Name="AccumulationRegister_Запасы" EntityType="StandardODATA.AccumulationRegister_Запасы_RecordType"/>
</EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>'''


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        registry_service.sync_metadata_to_registry(s, parse_metadata(SAMPLE))
        # спрятать одно поле, чтобы проверить exposed-фильтрацию
        nom = access.get_entity(s, "Catalog_Номенклатура")
        for p in nom.properties:
            if p.name == "СкрытоеПоле":
                p.exposed = False
        s.commit()
        yield s


def test_info_fields_types_and_operators(session):
    nom = access.get_entity(session, "Catalog_Номенклатура")
    info = entity_info(session, nom, with_stats=False)

    assert info["name"] == "Catalog_Номенклатура"
    assert info["key_property"] == "Ref_Key"
    f = info["fields"]
    assert "СкрытоеПоле" not in f
    assert f["Ref_Key"]["type"] == "uuid"
    assert f["Ref_Key"]["key"] is True
    assert f["Description"]["type"] == "string"
    assert "~" in f["Description"]["operators"] and "contains" in f["Description"]["operators"]
    assert f["Цена"]["type"] == "number"
    assert ">" in f["Цена"]["operators"]
    assert f["ЭтоНабор"]["type"] == "boolean"
    assert f["ДатаИзменения"]["type"] == "date-time"
    # guid-поля не поддерживают ~ / contains (проверяем на Ref_Key: он точно поле)
    assert "contains" not in f["Ref_Key"]["operators"]


def test_info_links(session):
    reg = access.get_entity(session, "AccumulationRegister_Запасы")
    info = entity_info(session, reg, with_stats=False)
    link = info["links"]["Номенклатура"]
    assert link["entity"] == "Catalog_Номенклатура"
    assert link["key_field"] == "Номенклатура_Key"
    assert link["type_field"] == "Номенклатура_Type"
    assert "Номенклатура_Type" not in info["fields"]  # занят ссылкой
    assert "Номенклатура_Key" in link["example_filter"]


def _activate(session, *names):
    for n in names:
        access.get_entity(session, n).is_active = True


def test_info_link_with_alias_source(session):
    # подмена: Номенклатура.ЕдиницаИзмерения → КлассификаторЕдиницИзмерения
    _activate(session, "Catalog_Номенклатура", "Catalog_ЕдиницыИзмерения",
              "Catalog_КлассификаторЕдиницИзмерения")
    entity_resolution.set_alias(
        session,
        source="Catalog_ЕдиницыИзмерения",
        target="Catalog_КлассификаторЕдиницИзмерения",
        owner="Catalog_Номенклатура",
        property_name="ЕдиницаИзмерения_Key",
    )
    session.commit()
    nom = access.get_entity(session, "Catalog_Номенклатура")
    info = entity_info(session, nom, with_stats=False)
    link = info["links"]["ЕдиницаИзмерения"]
    assert link["entity"] == "Catalog_КлассификаторЕдиницИзмерения"
    assert link["@source"] in ("override", "alias_prop")


def test_info_forbidden_target_drops_link(session):
    ed = access.get_entity(session, "Catalog_ЕдиницыИзмерения")
    access.disable_caching(session, ed.name)
    session.commit()
    nom = access.get_entity(session, "Catalog_Номенклатура")
    info = entity_info(session, nom, with_stats=False)
    # ссылка исчезает из links...
    assert "ЕдиницаИзмерения" not in info["links"]
    # ...а Key остаётся обычным guid-полем без семантики связи
    key_desc = info["fields"].get("ЕдиницаИзмерения_Key")
    if key_desc is not None:
        assert key_desc["type"] == "uuid"


def test_cache_stats_missing_and_created_table(session):
    nom = access.get_entity(session, "Catalog_Номенклатура")
    st = cache_stats(session, nom)
    assert st == {"rows": None, "table_exists": False}

    session.execute(text(f'CREATE TABLE "{nom.storage_table}" ("ref_key" TEXT PRIMARY KEY)'))
    session.execute(text(f'INSERT INTO "{nom.storage_table}" VALUES (:k)'), {"k": "abc"})
    session.commit()
    st = cache_stats(session, nom)
    assert st["table_exists"] is True
    assert st["rows"] == 1


def test_build_catalog_counts(session):
    entities = [
        access.get_entity(session, n)
        for n in ("Catalog_Номенклатура", "AccumulationRegister_Запасы")
    ]
    cat = build_catalog(session, entities)
    by_name = {c["name"]: c for c in cat}
    assert by_name["Catalog_Номенклатура"]["fields"] >= 6
    assert by_name["Catalog_Номенклатура"]["links"] == 1
    assert by_name["AccumulationRegister_Запасы"]["links"] == 1
