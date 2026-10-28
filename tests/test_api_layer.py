"""Шаг 10: слой чтения API (авторизация Bearer + /entities + /info + данные).

Гоняем HTTP-тесты через Flask test-client на SQLite с реальным сниппетом 1С
(Catalog_Номенклатура): metadata -> registry -> DDL -> loader -> токен/роль -> GET.

Проверяем ключевые договорённости:
- без токена / с плохим токеном → 401; admin-токен на data-API → 401;
- роль без grant на сущность → 403; неизвестная сущность → 404;
- формат ответа: data/sources/fields/pagination/warnings, без @odata;
- нулевой GUID → null в data; $filter/$select/$orderby/$top/$skip/count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from odata_cache.app_factory import create_app
from odata_cache.config import Settings
from odata_cache.db.base import Base
from odata_cache.metadata import parse_metadata
from odata_cache.models.auth import ApiToken, Role, RoleEntity
from odata_cache.models.registry import Entity
from odata_cache.services import access, ddl
from odata_cache.services.registry_service import STATE_SCHEDULED
from odata_cache.worker import loader

from test_real_sample import SAMPLE_META

FIXTURE = Path(__file__).parent.parent / "fixtures" / "Catalog_Номенклатура.json"


@pytest.fixture()
def env():
    """engine+session+наполненный кеш + Flask app в режиме api."""
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()

    # реестр из метаданных
    registry_service_sync(session)

    nom = access.get_entity(session, "Catalog_Номенклатура")
    props = list(nom.properties)
    battle = ddl.ensure_table(engine, nom, props)
    staging = ddl.ensure_staging(engine, nom, props)

    rows_raw = json.loads(FIXTURE.read_text(encoding="utf-8"))["value"]
    clean, _children = loader.split_collections(rows_raw)
    columns = {p.name: p.value_type for p in props}
    known = {p.name for p in props} | {"DataVersion"}
    rows, _warns = loader.normalize_rows(clean, columns, known)
    loader.load_batch(engine, staging, battle, rows, key_cols=["Ref_Key"])

    # права: роль «склад» видит Номенклатуру; роль «пустая» — ничего
    role = Role(name="warehouse", is_active=True)
    session.add(role)
    session.flush()
    session.add(RoleEntity(role_id=role.id, entity_id=nom.id, can_read=True))
    empty_role = Role(name="nobody", is_active=True)
    session.add(empty_role)
    session.commit()

    tokens = {}
    for label, rid in (("data", role.id), ("empty", empty_role.id), ("admin", None)):
        obj, secret = ApiToken.create(name=f"t-{label}", kind="admin" if label == "admin" else "data",
                                      role_id=rid)
        session.add(obj)
        session.commit()
        tokens[label] = secret

    settings = Settings(database_url="sqlite://", default_page_size=1, max_page_size=5)
    app = create_app("api", settings=settings)
    app.config["DB_SESSION_FACTORY"] = Session

    yield dict(engine=engine, session=session, tokens=tokens, client=app.test_client(),
               nom=nom)
    session.close()


def registry_service_sync(session):
    model = parse_metadata(SAMPLE_META)
    from odata_cache.services import registry_service

    registry_service.sync_metadata_to_registry(session, model)
    nom = access.get_entity(session, "Catalog_Номенклатура")
    nom.is_active = True
    nom.state = STATE_SCHEDULED
    session.commit()


H = lambda t: {"Authorization": f"Bearer {t}"}  # noqa: E731


# ---------------------------------------------------------------------------
# авторизация
# ---------------------------------------------------------------------------

def test_no_token_401(env):
    r = env["client"].get("/api/v1/entities")
    assert r.status_code == 401
    assert r.get_json()["errors"][0]["code"] == "no_token"


def test_bad_token_401(env):
    r = env["client"].get("/api/v1/entities", headers=H("odt_нет-такого"))
    assert r.status_code == 401
    assert r.get_json()["errors"][0]["code"] == "bad_token"


def test_admin_token_rejected_on_data_api(env):
    r = env["client"].get("/api/v1/entities", headers=H(env["tokens"]["admin"]))
    assert r.status_code == 401
    assert r.get_json()["errors"][0]["code"] == "wrong_kind"


def test_healthz_open(env):
    assert env["client"].get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# каталог и info
# ---------------------------------------------------------------------------

def test_catalog_only_granted(env):
    r = env["client"].get("/api/v1/entities", headers=H(env["tokens"]["data"]))
    assert r.status_code == 200
    names = [i["name"] for i in r.get_json()["data"]]
    assert names == ["Catalog_Номенклатура"]

    r2 = env["client"].get("/api/v1/entities", headers=H(env["tokens"]["empty"]))
    assert r2.get_json()["data"] == []


def test_info_forbidden_without_grant(env):
    r = env["client"].get("/api/v1/Catalog_Номенклатура/info",
                          headers=H(env["tokens"]["empty"]))
    assert r.status_code == 403


def test_info_unknown_404(env):
    r = env["client"].get("/api/v1/Catalog_ТакогоНет/info", headers=H(env["tokens"]["data"]))
    assert r.status_code == 404


def test_info_shape(env):
    r = env["client"].get("/api/v1/Catalog_Номенклатура/info",
                          headers=H(env["tokens"]["data"]))
    assert r.status_code == 200
    info = r.get_json()
    assert info["key_property"] == "Ref_Key"
    assert info["fields"]["Description"]["type"] == "string"
    assert info["stats"]["table_exists"] is True
    assert info["stats"]["rows"] == 2


# ---------------------------------------------------------------------------
# данные
# ---------------------------------------------------------------------------

def test_data_response_contract(env):
    tok = H(env["tokens"]["data"])
    r = env["client"].get("/api/v1/Catalog_Номенклатура?$top=2&count=1", headers=tok)
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"entity", "data", "sources", "fields", "pagination", "warnings"}
    assert body["entity"] == "Catalog_Номенклатура"
    assert len(body["data"]) == 2
    assert body["pagination"] == {"skip": 0, "top": 2, "returned": 2, "total": 2,
                                  "has_more": False}
    row = body["data"][0]
    # служебных OData-ключей нет; пустой GUID стал null'ём
    assert not any("@" in k for k in row)
    assert row["ВидАлкогольнойПродукции_Key"] is None
    assert row["ЕдиницаИзмерения_Key"] == "06b74a89-2a83-11ed-91a8-a068f8f3337c"
    assert body["fields"]["Description"]["type"] == "string"


def test_default_page_size_from_settings(env):
    # в env settings.default_page_size=1 → без $top вернётся 1 строка, has_more=True
    r = env["client"].get("/api/v1/Catalog_Номенклатура",
                          headers=H(env["tokens"]["data"]))
    body = r.get_json()
    assert body["pagination"]["top"] == 1 and body["pagination"]["has_more"] is True


def test_top_over_max_400(env):
    r = env["client"].get("/api/v1/Catalog_Номенклатура?$top=99",
                          headers=H(env["tokens"]["data"]))
    assert r.status_code == 400
    assert r.get_json()["errors"][0]["code"] == "bad_query"


def test_filter_sugar_and_select(env):
    tok = H(env["tokens"]["data"])
    q = "/api/v1/Catalog_Номенклатура?$filter{Description ~ 'покрывало'}&$select=Code,Description"
    r = env["client"].get(q, headers=tok)
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert len(body["data"]) == 1
    assert set(body["data"][0]) == {"Code", "Description"}


def test_filter_error_position(env):
    r = env["client"].get("/api/v1/Catalog_Номенклатура?$filter=НетТакогоПоля eq 1",
                          headers=H(env["tokens"]["data"]))
    assert r.status_code == 400
    err = r.get_json()["errors"][0]
    assert "НетТакогоПоля" in err["message"]


def test_orderby_and_skip(env):
    tok = H(env["tokens"]["data"])
    r = env["client"].get("/api/v1/Catalog_Номенклатура?$orderby=Code desc&$top=1",
                          headers=tok)
    first = r.get_json()["data"][0]["Code"]
    r2 = env["client"].get("/api/v1/Catalog_Номенклатура?$orderby=Code desc&$top=1&$skip=1",
                           headers=tok)
    second = r2.get_json()["data"][0]["Code"]
    assert first > second  # «ГРП…» идёт раньше «НФ…» при desc


def test_not_cached_warning(env):
    # вторая сущность активна, но таблица не создана → пустой ответ с warning
    ed = env["session"].scalar(select(Entity).where(Entity.name.like("%Characteristics")))
    if ed is None:
        pytest.skip("дочерняя сущность не завелась в этом фикстуре")
    ed.is_active = True
    env["session"].commit()
    role = env["session"].scalars(select(Role).where(Role.name == "warehouse")).one()
    env["session"].add(RoleEntity(role_id=role.id, entity_id=ed.id, can_read=True))
    env["session"].commit()
    r = env["client"].get(f"/api/v1/{ed.name}", headers=H(env["tokens"]["data"]))
    assert r.status_code == 200
    body = r.get_json()
    assert body["data"] == []
    assert any(w["code"] == "not_cached" for w in body["warnings"])
