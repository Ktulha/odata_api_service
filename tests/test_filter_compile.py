"""Тесты компилятора $filter → SQLAlchemy (шаг 7.2).

Проверяем на реальной SQLite-таблице: и генерацию SQL (bind-параметры),
и фактические результаты выборки, включая кириллические имена колонок.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, Table, Text, Float, Integer, select
from sqlalchemy.engine import create_engine

from odata_cache.filter_compile import (
    FieldInfo,
    FilterCompileError,
    compile_filter,
    make_field_infos,
    normalize_guid,
)

META = MetaData()
T = Table(
    "Catalog_Номенклатура", META,
    Column("Ref_Key", Text, primary_key=True),
    Column("Description", Text),
    Column("Code", Text),
    Column("Вес", Float),
    Column("ЭтоНабор", Integer),          # bool храним 0/1 (как в кэше)
    Column("ДатаИзменения", Text),        # datetime как ISO-текст (SQLite)
    Column("Склад_Key", Text),            # guid → текст нижним регистром
)

FIELDS = {
    "ref_key": FieldInfo("Ref_Key", "Ref_Key", "guid", column_obj=T.c.Ref_Key),
    "description": FieldInfo("Description", "Description", "string", column_obj=T.c.Description),
    "code": FieldInfo("Code", "Code", "string", column_obj=T.c.Code),
    "вес": FieldInfo("Вес", "Вес", "float", column_obj=T.c.Вес),
    "этонанор": FieldInfo("ЭтоНабор", "ЭтоНабор", "bool", column_obj=T.c.ЭтоНабор),
    "этонабор": FieldInfo("ЭтоНабор", "ЭтоНабор", "bool", column_obj=T.c.ЭтоНабор),
    "датаизменения": FieldInfo("ДатаИзменения", "ДатаИзменения", "datetime", column_obj=T.c.ДатаИзменения),
    "склад_key": FieldInfo("Склад_Key", "Склад_Key", "guid", column_obj=T.c.Склад_Key),
    "hidden": FieldInfo("Predefined", "Predefined", "bool", exposed=False, column_obj=T.c.ЭтоНабор),
}

G1 = "e912e33c-d7a5-11ed-96d7-000c29399934"
G2 = "849028df-d466-11ed-96d7-000c29399934"
ZERO = "00000000-0000-0000-0000-000000000000"

ROWS = [
    dict(Ref_Key=G1, Description="Покрывало меховое белое", Code="НФ-00000335",
         Вес=1.5, ЭтоНабор=0, ДатаИзменения="2025-09-08T13:07:12", Склад_Key=G2),
    dict(Ref_Key=G2, Description="Халат махровый", Code="НФ-00000336",
         Вес=0.0, ЭтоНабор=1, ДатаИзменения="2025-01-01T00:00:00", Склад_Key=None),
    dict(Ref_Key=ZERO, Description="Покрывало %тест_", Code="НФ-00000337",
         Вес=3.0, ЭтоНабор=0, ДатаИзменения="2026-05-05T10:00:00", Склад_Key=G2),
]


@pytest.fixture()
def conn():
    engine = create_engine("sqlite://")
    META.create_all(engine)
    with engine.connect() as c:
        for r in ROWS:
            c.execute(T.insert().values(**r))
        c.commit()
        yield c
    engine.dispose()


def run(conn, source: str):
    crit = compile_filter(source, FIELDS)
    stmt = select(T.c.Ref_Key).where(crit).order_by(T.c.Code)
    return [row[0] for row in conn.execute(stmt)]


# ---------------------------------------------------------------------------
# базовые операторы + приведение типов
# ---------------------------------------------------------------------------

def test_eq_string_and_case_insensitive_field(conn):
    assert run(conn, "Description eq 'Халат махровый'") == [G2]
    assert run(conn, "description = 'Халат махровый'") == [G2]  # имя поля без регистра


def test_numeric_coercion_from_string(conn):
    assert run(conn, "Вес > '1'") == [G1, ZERO]      # строковый литерал → float
    assert run(conn, "Вес >= 1.5 and Вес <= 3") == [G1, ZERO]


def test_bool_coercion(conn):
    assert run(conn, "ЭтоНабор = true") == [G2]
    assert run(conn, "ЭтоНабор <> 1") == [G1, ZERO]


def test_datetime_bare_date_means_midnight(conn):
    assert run(conn, "ДатаИзменения ge '2025-09-08'") == [G1, ZERO]
    assert run(conn, "ДатаИзменения lt '2025-09-08'") == [G2]


def test_guid_normalized_in_filter(conn):
    # guid(...) из OData-нотации и верхний регистр — приводим к канону
    assert run(conn, f"Склад_Key eq guid'{G2}'") == [G2, ZERO]
    assert run(conn, f"Склад_Key = '{G2.upper()}'") == [G2, ZERO]


# ---------------------------------------------------------------------------
# in / null / not
# ---------------------------------------------------------------------------

def test_in_list_of_keys(conn):
    assert run(conn, f"$filter{{Ref_Key in [{G1}, {ZERO}]}}") == [G1, ZERO]


def test_not_in(conn):
    assert run(conn, f"Ref_Key not in ('{G1}')") == [G2, ZERO]


def test_is_null_on_guid_column(conn):
    assert run(conn, "Склад_Key is null") == [G2]
    assert run(conn, "not (Склад_Key is null)") == [G1, ZERO]
    assert run(conn, "Склад_Key is not null") == [G1, ZERO]


def test_empty_in_list(conn):
    assert run(conn, "Code in ()") == []
    assert len(run(conn, "Code not in ()")) == 3


def test_like_keyword_is_contains(conn):
    # 'like' из эскиза пользователя = contains (регистронезависимый)
    assert run(conn, "Description like 'покрывало'") == [G1, ZERO]


def test_guid_uppercase_normalized(conn):
    # в кэше guid хранится нижним регистром; фильтр с верхнего — приводится
    assert run(conn, f"Ref_Key eq '{G1.upper()}'") == [G1]


# ---------------------------------------------------------------------------
# like-операторы и экранирование
# ---------------------------------------------------------------------------

def test_sugar_contains_ci(conn):
    assert run(conn, "Description ~ 'покрывало'") == [G1, ZERO]
    assert run(conn, "contains(Description, 'МАХРОВЫЙ')") == [G2]


def test_like_percent_is_literal(conn):
    # '%' в значении — обычный символ, не wildcard
    assert run(conn, "Description ~ '%тест_'") == [ZERO]


def test_startswith_endswith(conn):
    assert run(conn, "startswith(Code, 'НФ-0000033')") == [G1, G2, ZERO]
    assert run(conn, "endswith(Description, 'белое')") == [G1]


def test_user_sketch_example(conn):
    # дословный эскиз пользователя: $filter{... in [...], ... like '...' and ...}
    q = (f"$filter{{Ref_Key in [{G1},{G2}],"
         f"Description like 'халат' and Description like 'махровый'}}")
    assert run(conn, q) == [G2]
    # канонический вариант с and вместо запятой — тот же результат
    q2 = f"Ref_Key in ({G1},{G2}) and Description ~ 'халат' and Description ~ 'махровый'"
    assert run(conn, q2) == [G2]


# ---------------------------------------------------------------------------
# ошибки → 400 (FilterCompileError)
# ---------------------------------------------------------------------------

def test_unknown_field_error():
    with pytest.raises(FilterCompileError, match="неизвестное поле"):
        compile_filter("NoSuchField eq 1", FIELDS)


def test_hidden_field_error():
    with pytest.raises(FilterCompileError, match="недоступно"):
        compile_filter("Predefined eq true", FIELDS)


def test_bad_number_error():
    with pytest.raises(FilterCompileError, match="не число"):
        compile_filter("Вес > 'abc'", FIELDS)


def test_bad_guid_error():
    with pytest.raises(FilterCompileError, match="не GUID"):
        compile_filter(f"Склад_Key eq '{'zzz'}'", FIELDS)


def test_syntax_error_propagates_position():
    with pytest.raises(FilterCompileError) as ei:
        compile_filter("Description eq", FIELDS)
    assert ei.value.pos is not None


def test_normalize_guid_helper():
    assert normalize_guid("guid'E912E33C-D7A5-11ED-96D7-000C29399934'") == G1
    assert normalize_guid("{E912E33C-D7A5-11ED-96D7-000C29399934}") == G1
    assert normalize_guid("not-a-guid") is None


def test_make_field_infos_from_table():
    props = [type("P", (), {"name": "Description", "column_name": "Description",
                            "value_type": "string", "exposed": True})()]
    idx = make_field_infos(T, props)
    assert "description" in idx
    crit = compile_filter("Description ~ 'халат'", idx)
    assert crit is not None
