"""Тесты DSL-парсера $filter (смешанный стиль, Q4-a «а»)."""

import pytest

from odata_cache.filter_dsl import (
    Compare,
    FilterSyntaxError,
    InList,
    IsNull,
    Logic,
    Not,
    iter_fields,
    parse_filter,
)


def test_sugar_eq_and_tilde():
    node = parse_filter("Description ~ 'халат' and Code = 'НФ-00000335'")
    assert isinstance(node, Logic) and node.op == "and"
    left, right = node.children
    assert isinstance(left, Compare)
    assert (left.field_name, left.op, left.value) == ("Description", "contains", "халат")
    assert (right.field_name, right.op, right.value) == ("Code", "eq", "НФ-00000335")


def test_in_list_of_guids_braces():
    # эскиз пользователя: {Ref_Key in [..]} → принимаем guid'..' в () и {}
    src = ("{Ref_Key in (guid'e912e33c-d7a5-11ed-96d7-000c29399934', "
           "guid'849028df-d466-11ed-96d7-000c29399934') "
           "and Description ~ 'махровый'}")
    node = parse_filter(src)
    assert isinstance(node, Logic) and node.op == "and"
    in_node = node.children[0]
    assert isinstance(in_node, InList) and not in_node.negated
    assert in_node.field_name == "Ref_Key"
    assert len(in_node.items) == 2
    assert in_node.items[0] == "e912e33c-d7a5-11ed-96d7-000c29399934"


def test_odata_canon_ops():
    node = parse_filter("Вес ge 0 and Количество lt 10.5")
    a, b = node.children
    assert (a.op, a.value) == ("ge", 0)
    assert (b.op, b.value) == ("lt", 10.5)


def test_is_null_variants():
    n1 = parse_filter("Артикул is null")
    assert isinstance(n1, IsNull) and not n1.negated
    n2 = parse_filter("Штрихкод is not null")
    assert isinstance(n2, IsNull) and n2.negated


def test_functions():
    node = parse_filter("contains(НаименованиеПолное,'мех')")
    assert isinstance(node, Compare) and node.op == "contains"
    assert node.value == "мех"


def test_not_and_nested_parens():
    node = parse_filter("not (IsFolder = true or ЭтоНабор = true)")
    assert isinstance(node, Not)
    assert isinstance(node.child, Logic) and node.child.op == "or"


def test_bool_literals_and_ne():
    node = parse_filter("DeletionMark <> true")
    assert isinstance(node, Compare) and node.op == "ne" and node.value is True


def test_datetime_literal():
    node = parse_filter("ДатаИзменения gt datetime'2025-09-08T13:07:12'")
    assert node.value == "2025-09-08T13:07:12"  # type: ignore[attr-defined]


def test_escaped_quote_in_string():
    node = parse_filter("Description = 'ткань ''махра'''")
    assert node.value == "ткань 'махра'"  # type: ignore[attr-defined]


def test_iter_fields_collects_all():
    node = parse_filter("A = 1 and (B ~ 'x' or C in (1,2)) and not D is null")
    assert iter_fields(node) == ["A", "B", "C", "D"]


# --- ошибки ---------------------------------------------------------------

@pytest.mark.parametrize("src,pos_hint", [
    ("Description ~", 13),                 # нет значения
    ("= 'x'", 0),                          # нет поля
    ("A = 'x' B = 1", None),               # нет and/or
    ("(A = 1", None),                      # незакрытая скобка
    ("A in 1,2", None),                    # in без скобок
    ("A =~ 'x'", None),                    # мусорный оператор
])
def test_syntax_errors(src, pos_hint):
    with pytest.raises(FilterSyntaxError) as ei:
        parse_filter(src)
    assert ei.value.position >= 0
    if pos_hint is not None:
        assert ei.value.position == pos_hint


def test_depth_limit():
    deep = " ".join(["("] * 9 + ["A = 1"] + [")"] * 9)
    with pytest.raises(FilterSyntaxError):
        parse_filter(deep)


def test_length_limit():
    with pytest.raises(FilterSyntaxError):
        parse_filter("A ~ '" + "x" * 3000 + "'")
