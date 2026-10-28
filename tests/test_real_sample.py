"""Шаг 8-проверка: полный конвейер на РЕАЛЬНОМ сниппете данных 1С.

fixtures/Catalog_Номенклатура.json — сырой ответ
  /odata/standard.odata/Catalog_Номенклатура?$format=json
(фрагмент реального ответа УНФ, сохранён как есть).

Гоняем без сети: metadata -> registry -> DDL -> loader(staging/upsert) -> filter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from odata_cache.db.base import Base
from odata_cache.filter_compile import compile_filter, make_field_infos
from odata_cache.metadata import parse_metadata
from odata_cache.models.registry import Entity
from odata_cache.services import ddl, registry_service
from odata_cache.worker import loader

FIXTURE = Path(__file__).parent.parent / "fixtures" / "Catalog_Номенклатура.json"

# $metadata для Catalog_Номенклатура по реальной структуре ответа (ключевые поля;
# остальные добавим «как в данных» — OpenType-сущность)
SAMPLE_META = '''<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
<edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata" m:DataServiceVersion="3.0">
<Schema xmlns="http://schemas.microsoft.com/ado/2009/11/edm" Namespace="StandardODATA">
<EntityType Name="Catalog_Номенклатура" OpenType="true">
<Key><PropertyRef Name="Ref_Key"/></Key>
<Property Name="Ref_Key" Type="Edm.Guid" Nullable="false"/>
<Property Name="DeletionMark" Type="Edm.Boolean" Nullable="true"/>
<Property Name="Parent_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="IsFolder" Type="Edm.Boolean" Nullable="true"/>
<Property Name="Code" Type="Edm.String" Nullable="true"/>
<Property Name="Description" Type="Edm.String" Nullable="true"/>
<Property Name="Артикул" Type="Edm.String" Nullable="true"/>
<Property Name="ВариантОграниченияСертификата" Type="Edm.String" Nullable="true"/>
<Property Name="Вес" Type="Edm.Double" Nullable="true"/>
<Property Name="ЕдиницаИзмерения_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="ВидАлкогольнойПродукции_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="ДатаИзменения" Type="Edm.DateTime" Nullable="true"/>
<Property Name="ИспользоватьХарактеристики" Type="Edm.Boolean" Nullable="true"/>
<Property Name="КатегорияНоменклатуры_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Склад_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="МетодОценки" Type="Edm.String" Nullable="true"/>
<Property Name="ТипНоменклатуры" Type="Edm.String" Nullable="true"/>
<Property Name="ДополнительныеРеквизиты" Type="Collection(StandardODATA.Catalog_НоменклатураCharacteristics)" Nullable="false"/>
</EntityType>
<EntityType Name="Catalog_НоменклатураCharacteristics">
<Key><PropertyRef Name="_ID"/><PropertyRef Name="LineNumber"/></Key>
<Property Name="_ID" Type="Edm.Guid" Nullable="false"/>
<Property Name="LineNumber" Type="Edm.Int64" Nullable="false"/>
<Property Name="Свойство_Key" Type="Edm.Guid" Nullable="true"/>
<Property Name="Значение" Type="Edm.String" Nullable="true"/>
<Property Name="Значение_Type" Type="Edm.String" Nullable="true"/>
<Property Name="ТекстоваяСтрока" Type="Edm.String" Nullable="true"/>
</EntityType>
<EntityContainer Name="container">
<EntitySet Name="Catalog_Номенклатура" EntityType="StandardODATA.Catalog_Номенклатура"/>
</EntityContainer>
</Schema></edmx:DataServices></edmx:Edmx>'''


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    return sessionmaker(bind=engine)()


@pytest.fixture(scope="module")
def sample_rows():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["value"]


def _load_registry(session):
    model = parse_metadata(SAMPLE_META)
    registry_service.sync_metadata_to_registry(session, model)
    ent = session.scalar(select(Entity).where(Entity.name == "Catalog_Номенклатура"))
    assert ent is not None
    return ent


def test_pipeline_load_real_snippet(engine, session, sample_rows):
    ent = _load_registry(session)
    props = list(ent.properties)
    battle = ddl.ensure_table(engine, ent, props)
    staging = ddl.ensure_staging(engine, ent, props)

    clean, children = loader.split_collections(sample_rows)
    columns = {p.name: p.value_type for p in props}
    known = {p.name for p in props} | {"DataVersion"}
    rows, warns = loader.normalize_rows(clean, columns, known)

    res = loader.load_batch(engine, staging, battle, rows, key_cols=["Ref_Key"])
    assert res["inserted"] == 2, res

    # дочерняя таблица коллекции «ДополнительныеРеквизиты»
    child = session.scalar(select(Entity).where(Entity.name == "Catalog_НоменклатураCharacteristics"))
    if child is not None:
        cprops = list(child.properties)
        cbattle = ddl.ensure_table(engine, child, cprops)
        crow_rows, _ = loader.normalize_rows(
            [item for _parent, item in children.get("ДополнительныеРеквизиты", [])],
            {q.name: q.value_type for q in cprops},
            {q.name for q in cprops} | {"Ref_Key"})
        for r, (_parent, _item) in zip(crow_rows, children.get("ДополнительныеРеквизиты", [])):
            r["_ID"] = _item.get("Ref_Key")   # owner = родитель
        cres = loader.load_batch(engine, cbattle, cbattle, crow_rows,
                                 key_cols=["_ID", "LineNumber"], full=False)
        assert cres["inserted"] == 1, cres
    # неизвестных полей быть не должно: всё включено в «метаданные»
    assert not [w for w in res["warnings"] + warns if "не в схеме" in w]

    with engine.connect() as conn:
        got = conn.execute(text('SELECT "Description", "ТипНоменклатуры", "Вес" '
                                f'FROM "{battle.name}" ORDER BY "Code"')).all()
    assert got[0][0] == "Текстиль для дома"          # группа, IsFolder
    assert "Покрывало" in got[1][0]                   # кавычки внутри значения ок
    assert got[1][2] == 0.0

    # guid нормализован, нулевой guid сохранён как '0'*36 (фильтруемость пустых ссылок)
    with engine.connect() as conn:
        eiu = conn.execute(text('SELECT "ЕдиницаИзмерения_Key", "ВидАлкогольнойПродукции_Key" '
                                f'FROM "{battle.name}" WHERE "Code"=\'НФ-00000335\'')).one()
    assert eiu[0] == "06b74a89-2a83-11ed-91a8-a068f8f3337c"
    assert eiu[1] == "00000000-0000-0000-0000-000000000000"

    # datetime распознан
    with engine.connect() as conn:
        d = conn.execute(text(f'SELECT "ДатаИзменения" FROM "{battle.name}" '
                              'WHERE "Code"=\'НФ-00000335\'')).scalar()
    assert str(d).startswith("2025-09-08")


def test_upsert_and_stale_removal(engine, session, sample_rows):
    ent = _load_registry(session)
    props = list(ent.properties)
    battle = ddl.ensure_table(engine, ent, props)
    staging = ddl.ensure_staging(engine, ent, props)
    columns = {p.name: p.value_type for p in props}
    known = {p.name for p in props} | {"DataVersion"}

    clean, _ = loader.split_collections(sample_rows)
    rows, _ = loader.normalize_rows(clean, columns, known)
    loader.load_batch(engine, staging, battle, rows, key_cols=["Ref_Key"])

    # повторная загрузка с изменённым описанием и удалением одной записи
    upd = [dict(r) for r in rows]
    upd[1]["Description"] = "Текстиль (переименован)"
    res = loader.load_batch(engine, staging, battle, [upd[1]], key_cols=["Ref_Key"], full=True)
    assert res["updated"] == 1 and res["deleted"] == 1, res
    with engine.connect() as conn:
        n = conn.execute(text(f'SELECT COUNT(*) FROM "{battle.name}"')).scalar()
        desc = conn.execute(text(f'SELECT "Description" FROM "{battle.name}" '
                                 "WHERE \"Code\"='ГРП-0001'")).scalar()
    assert n == 1 and desc == "Текстиль (переименован)"


def test_filter_on_real_columns(engine, session, sample_rows):
    """Кириллические колонки + sugar-фильтры из DSL работают на реальных данных."""
    ent = _load_registry(session)
    props = list(ent.properties)
    battle = ddl.ensure_table(engine, ent, props)
    staging = ddl.ensure_staging(engine, ent, props)
    clean, _ = loader.split_collections(sample_rows)
    rows, _ = loader.normalize_rows(clean, {p.name: p.value_type for p in props},
                                    {p.name for p in props} | {"DataVersion"})
    loader.load_batch(engine, staging, battle, rows, key_cols=["Ref_Key"])

    fields = make_field_infos(battle, props)
    q = text  # noqa: F841
    # «~» — подстрока без регистра: в данных «Покрывало» с большой буквы
    crit = compile_filter("Вес >= 0 and Description ~ 'покрывало'", fields)
    stmt = select(battle).where(crit)
    from sqlalchemy import func
    with engine.connect() as conn:
        found = conn.execute(stmt).all()
    assert len(found) == 1 and "Покрывало" in found[0]._mapping["Description"]

    crit2 = compile_filter("Ref_Key in ['e912e33c-d7a5-11ed-96d7-000c29399934']", fields)
    with engine.connect() as conn:
        assert len(conn.execute(select(battle).where(crit2)).all()) == 1


def test_open_type_unknown_field_warns(engine, session, sample_rows):
    """Поле сверх схемы (OpenType) не роняет загрузку, а уходит в warnings."""
    ent = _load_registry(session)
    props = list(ent.properties)
    battle = ddl.ensure_table(engine, ent, props)
    staging = ddl.ensure_staging(engine, ent, props)
    row = dict(sample_rows[1])
    row["НовоеПолеИзЖизни"] = "?"
    rows, warns = loader.normalize_rows([row], {p.name: p.value_type for p in props},
                                        {p.name for p in props})
    assert any("НовоеПолеИзЖизни" in w for w in warns)
    res = loader.load_batch(engine, staging, battle, rows, key_cols=["Ref_Key"])
    assert res["inserted"] == 1
