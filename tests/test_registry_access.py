"""Шаг 4: реестр из $metadata + политика доступа/кэширования."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from odata_cache.db.base import Base
from odata_cache.metadata import parse_metadata
from odata_cache.models.auth import Role, RoleEntity
from odata_cache.models.registry import Entity
from odata_cache.services import access, registry_service

SAMPLE = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0" m:MaxDataServiceVersion="3.0">
<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">
<EntityType Name="Catalog_Номенклатура">
<Key><PropertyRef Name="Key"/></Key>
<Property Name="Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
<Property Name="ЭтоСлужебное" Type="Edm.Boolean" Nullable="true"/>
</EntityType>
<EntityType Name="Catalog_Организация">
<Key><PropertyRef Name="Key"/></Key>
<Property Name="Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="AccumulationRegister_ЗапасыПереданныеВРемонте" OpenType="true">
<Key><PropertyRef Name="Recorder"/><PropertyRef Name="Recorder_Type"/></Key>
<Property Name="Recorder" Type="Edm.String" Nullable="false"/>
<Property Name="RecordSet" Type="Collection(StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте_RowType)" Nullable="false"/>
<Property Name="Recorder_Type" Type="Edm.String" Nullable="false"/>
</EntityType>
<EntityType Name="AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType" OpenType="true">
<Key><PropertyRef Name="Recorder"/><PropertyRef Name="LineNumber"/><PropertyRef Name="Recorder_Type"/></Key>
<Property Name="Recorder" Type="Edm.String" Nullable="false"/>
<Property Name="Period" Type="Edm.DateTime" Nullable="true"/>
<Property Name="LineNumber" Type="Edm.Int64" Nullable="false"/>
<Property Name="Организация_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Номенклатура_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Серия_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Количество" Type="Edm.Double" Nullable="true"/>
<Property Name="Recorder_Type" Type="Edm.String" Nullable="false"/>
<NavigationProperty Name="Организация" Relationship="StandardODATA.R_Организация" FromRole="Begin" ToRole="End"/>
<NavigationProperty Name="Номенклатура" Relationship="StandardODATA.R_Номенклатура" FromRole="Begin" ToRole="End"/>
<NavigationProperty Name="Серия" Relationship="StandardODATA.R_Серия" FromRole="Begin" ToRole="End"/>
</EntityType>
<Association Name="R_Организация">
<End Type="StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_Организация" Role="End" Multiplicity="0..1"/>
</Association>
<Association Name="R_Номенклатура">
<End Type="StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_Номенклатура" Role="End" Multiplicity="0..1"/>
</Association>
<Association Name="R_Серия">
<End Type="StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Series_СерииНоменклатуры" Role="End" Multiplicity="0..1"/>
</Association>
<EntityContainer Name="container">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
<EntitySet Name="Catalog_Организация" EntityType="StandardODATA.Catalog_Организация"/>
<EntitySet Name="AccumulationRegister_ЗапасыПереданныеВРемонте" EntityType="StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте"/>
<EntitySet Name="AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType" EntityType="StandardODATA.AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType"/>
</EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>'''


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


def test_sync_registers_disabled_by_default(session):
    model = parse_metadata(SAMPLE)
    res = registry_service.sync_metadata_to_registry(session, model)
    session.commit()

    assert sorted(res.created) == [
        "AccumulationRegister_ЗапасыПереданныеВРемонте",
        "AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType",
        "Catalog_Номенклатура",
        "Catalog_Организация",
    ]
    # правило «не грузим сразу»: всё discovered + is_active=False
    for e in session.scalars(select(Entity)):
        assert e.state == registry_service.STATE_DISCOVERED
        assert e.is_active is False
    # идемпотентность повторного разбора
    res2 = registry_service.sync_metadata_to_registry(session, model)
    session.commit()
    assert res2.created == [] and res2.updated == []
    assert len(res2.unchanged) == 4
    # связи ref_entity_id не должны «теряться» между проходами
    nom = access.get_entity(session, "Catalog_Номенклатура")
    org = access.get_entity(session, "Catalog_Организация")
    by_name = {p.name: p for p in nom.properties}
    if "Организация_Key" in by_name:
        assert by_name["Организация_Key"].ref_kind == "ref"
        assert by_name["Организация_Key"].ref_entity_id == org.id


def test_links_resolved_from_metadata(session):
    registry_service.sync_metadata_to_registry(session, parse_metadata(SAMPLE))
    session.commit()
    rec = access.get_entity(session, "AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType")
    props = {p.name: p for p in rec.properties}

    # Номенклатура_Key -> сущность Catalog_Номенклатура (FK в реестре)
    nom = props["Номенклатура_Key"]
    assert nom.ref_kind == "ref"
    target = session.get(Entity, nom.ref_entity_id)
    assert target is not None and target.name == "Catalog_Номенклатура"

    org = props["Организация_Key"]
    assert session.get(Entity, org.ref_entity_id).name == "Catalog_Организация"

    # Серия: целевой EntityType нет среди EntitySet → ссылки нет, колонка просто Guid
    ser = props["Серия_Key"]
    assert ser.ref_entity_id is None
    # RecordSet (коллекция) не стал колонкой
    assert "RecordSet" not in props
    # ключи и типы
    assert props["LineNumber"].value_type == "int"
    assert props["Количество"].value_type == "float"
    assert props["Period"].value_type == "datetime"
    assert props["Recorder"].is_key is True


def test_access_rules(session):
    registry_service.sync_metadata_to_registry(session, parse_metadata(SAMPLE))
    role = Role(name="wms"); session.add(role); session.commit()

    # выключено глобально → forbidden
    with pytest.raises(access.AccessError) as err:
        access.require_entity_access(session, name="Catalog_Номенклатура", role_id=role.id)
    assert err.value.reason == "forbidden"

    # включили через расписание, но grant нет → not_granted
    access.enable_caching(session, "Catalog_Номенклатура", via_schedule=True)
    session.commit()
    with pytest.raises(access.AccessError) as err:
        access.require_entity_access(session, name="Catalog_Номенклатура", role_id=role.id)
    assert err.value.reason == "not_granted"

    # выдали grant → доступно
    session.add(RoleEntity(role_id=role.id, entity_id=access.get_entity(session, "Catalog_Номенклатура").id))
    session.commit()
    ent = access.require_entity_access(session, name="catalog_номенклатура", role_id=role.id)
    assert ent.name == "Catalog_Номенклатура"
    assert access.allowed_entities(session, role.id) == {"Catalog_Номенклатура"}

    # отключили → снова forbidden и исчезает из allowed
    access.disable_caching(session, "Catalog_Номенклатура")
    session.commit()
    assert access.allowed_entities(session, role.id) == set()
    with pytest.raises(access.AccessError):
        access.require_entity_access(session, name="Catalog_Номенклатура", role_id=role.id)

    # несуществующая сущность → unknown (а не «есть такая, но нельзя»)
    with pytest.raises(access.AccessError) as err:
        access.require_entity_access(session, name="ТайнаяСущность", role_id=role.id)
    assert err.value.reason == "unknown"


def test_on_demand_pulls_allowed_dependencies_only(session):
    registry_service.sync_metadata_to_registry(session, parse_metadata(SAMPLE))
    session.commit()

    # разрешаем кеш только для регистра-записей и Номенклатуры; Организация — запрещена
    access.enable_caching(session, "AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType", via_schedule=False)
    access.enable_caching(session, "Catalog_Номенклатура", via_schedule=False)
    session.commit()

    rec = access.get_entity(session, "AccumulationRegister_ЗапасыПереданныеВРемонте_RecordType")
    deps = access.cacheable_dependency_names(session, rec)
    assert deps == ["Catalog_Номенклатура"]      # Организация не активна → не тянем
    assert access.needs_initial_load(rec) is True

    rec.last_synced_at = "2026-10-28T00:00:00+00:00"
    session.commit()
    assert access.needs_initial_load(rec) is False
