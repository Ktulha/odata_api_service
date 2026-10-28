"""Шаг 6: план загрузки без $expand — ссылки качаем только разрешённые сущности."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from odata_cache.db.base import Base
from odata_cache.metadata import parse_metadata
from odata_cache.services import access, load_plan, registry_service

SAMPLE = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0" m:MaxDataServiceVersion="3.0">
<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">
<EntityType Name="Catalog_Номенклатура">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
<Property Name="ЕдиницаИзмерения_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Склад_Key" Type="Edm.Guid" Nullable="true"/>
<NavigationProperty Name="ЕдиницаИзмерения" Relationship="StandardODATA.R_Ед" FromRole="Begin" ToRole="End"/>
<NavigationProperty Name="Склад" Relationship="StandardODATA.R_Склад" FromRole="Begin" ToRole="End"/>
</EntityType>
<EntityType Name="Catalog_ЕдиницыИзмерения">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityType Name="Catalog_Склады">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
</EntityType>
<Association Name="R_Ед">
<End Type="StandardODATA.Catalog_Номенклатура" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_ЕдиницыИзмерения" Role="End" Multiplicity="0..1"/>
</Association>
<Association Name="R_Склад">
<End Type="StandardODATA.Catalog_Номенклатура" Role="Begin" Multiplicity="*"/>
<End Type="StandardODATA.Catalog_Склады" Role="End" Multiplicity="0..1"/>
</Association>
<EntityContainer Name="Container" m:IsDefaultEntityContainer="true">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
<EntitySet Name="Catalog_ЕдиницыИзмерения" EntityType="StandardODATA.Catalog_ЕдиницыИзмерения"/>
<EntitySet Name="Catalog_Склады" EntityType="StandardODATA.Catalog_Склады"/>
</EntityContainer>
</Schema>
</edmx:DataServices>
</edmx:Edmx>'''


@pytest.fixture()
def session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as s:
        yield s


def _registry(session):
    registry_service.sync_metadata_to_registry(session, parse_metadata(SAMPLE))
    session.commit()


def test_plan_includes_only_allowed_dependencies(session):
    _registry(session)
    # разрешаем Номенклатуру и ЕдиницыИзмерения; Склады — запрещены (discovered, не активны)
    access.enable_caching(session, "Catalog_Номенклатура", via_schedule=False)
    access.enable_caching(session, "Catalog_ЕдиницыИзмерения", via_schedule=False)
    session.commit()

    plan = load_plan.build_load_plan(session, "Catalog_Номенклатура")
    assert plan.names() == ["Catalog_Номенклатура", "Catalog_ЕдиницыИзмерения"]
    assert plan.skipped_forbidden == {"Catalog_Склады"}

    root_task = plan.tasks[0]
    assert root_task.root and root_task.key_values is None  # полный пакет
    dep = plan.tasks[1]
    assert dep.pending_keys is True  # ключи придут из строк родителя
    assert dep.via.startswith("ref:Catalog_Номенклатура.")


def test_filter_expression_and_seeding(session):
    _registry(session)
    access.enable_caching(session, "Catalog_Номенклатура", via_schedule=True)
    access.enable_caching(session, "Catalog_ЕдиницыИзмерения", via_schedule=True)
    session.commit()

    plan = load_plan.build_load_plan(
        session, "Catalog_Номенклатура",
        seed_keys={"Catalog_ЕдиницыИзмерения": ["aaa", "bbb", "aaa"]},
    )
    dep = next(t for t in plan.tasks if t.entity_name == "Catalog_ЕдиницыИзмерения")
    assert dep.pending_keys is False
    expr = dep.filter_expression("Ref_Key")
    assert expr == "Ref_Key in ('aaa', 'bbb')"

    # доливка ключей после загрузки родителя
    load_plan.merge_seed_keys(plan, {"Catalog_ЕдиницыИзмерения": ["ccc"]})
    assert dep.key_values == ["aaa", "bbb", "ccc"]


def test_extract_ref_keys_skips_zero_guid(session):
    _registry(session)
    nom = access.get_entity(session, "Catalog_Номенклатура")
    ed = access.get_entity(session, "Catalog_ЕдиницыИзмерения")
    rows = [
        {"ЕдиницаИзмерения_Key": "06b74a89-2a83-11ed-91a8-a068f8f3337c",
         "Склад_Key": "00000000-0000-0000-0000-000000000000"},
        {"ЕдиницаИзмерения_Key": "06b74a89-2a83-11ed-91a8-a068f8f3337c",
         "Склад_Key": None},
    ]
    got = load_plan.extract_ref_key_values(rows, nom)
    assert got == {ed.id: {"06b74a89-2a83-11ed-91a8-a068f8f3337c"}}


def test_unknown_root_raises(session):
    _registry(session)
    with pytest.raises(ValueError):
        load_plan.build_load_plan(session, "Каталог_Несуществующий")
