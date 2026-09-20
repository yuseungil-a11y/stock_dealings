"""파라미터 검증/변환 단위테스트."""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from stock_svr.algo.params import ParamError, ParamSet, coerce, enum_choices, validate, validate_all


def pdef(**kw):
    base = {"param_key": "k", "label": "테스트", "value_type": "int",
            "default_value": "0", "min_value": None, "max_value": None,
            "enum_options": None, "unit": None, "description": None}
    base.update(kw)
    return base


# -- int ---------------------------------------------------------------- #
def test_int_ok_and_range():
    d = pdef(value_type="int", min_value="0", max_value="100")
    assert validate(d, "50") == "50"
    assert validate(d, 7) == "7"
    with pytest.raises(ParamError):
        validate(d, "101")
    with pytest.raises(ParamError):
        validate(d, "-1")
    with pytest.raises(ParamError):
        validate(d, "abc")


# -- decimal ------------------------------------------------------------ #
def test_decimal():
    d = pdef(value_type="decimal", min_value="-99", max_value="0")
    assert validate(d, "-15") == "-15"
    assert validate(d, "-3.50") == "-3.5"
    with pytest.raises(ParamError):
        validate(d, "1")


# -- bool --------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", [
    ("1", "1"), ("0", "0"), ("true", "1"), ("False", "0"), ("Y", "1"), ("n", "0"),
])
def test_bool(raw, expected):
    assert validate(pdef(value_type="bool"), raw) == expected


def test_bool_invalid():
    with pytest.raises(ParamError):
        validate(pdef(value_type="bool"), "maybe")


# -- enum --------------------------------------------------------------- #
def test_enum():
    d = pdef(value_type="enum", enum_options="3:시장가,0:지정가(보통),6:최유리지정가")
    assert validate(d, "3") == "3"
    with pytest.raises(ParamError):
        validate(d, "9")
    assert enum_choices(d)[0] == ("3", "시장가")


# -- time --------------------------------------------------------------- #
def test_time():
    d = pdef(value_type="time")
    assert validate(d, "9:05") == "09:05"
    assert validate(d, "1515") == "15:15"
    assert validate(d, "") == ""
    with pytest.raises(ParamError):
        validate(d, "99:99")


# -- string ------------------------------------------------------------- #
def test_string_length():
    d = pdef(value_type="string")
    assert validate(d, "005930,000660") == "005930,000660"
    with pytest.raises(ParamError):
        validate(d, "x" * 101)


# -- coerce / ParamSet -------------------------------------------------- #
def test_coerce_types():
    assert coerce(pdef(value_type="int"), "12") == 12
    assert coerce(pdef(value_type="decimal"), "0.5") == Decimal("0.5")
    assert coerce(pdef(value_type="bool"), "1") is True
    assert coerce(pdef(value_type="time"), "15:15") == _dt.time(15, 15)


def test_param_set_defaults_and_types():
    defs = [
        pdef(param_key="max_steps", value_type="int", default_value="3"),
        pdef(param_key="drop_pct", value_type="decimal", default_value="10"),
        pdef(param_key="exclude_etf", value_type="bool", default_value="1"),
        pdef(param_key="liquidate_time", value_type="time", default_value="15:15"),
        pdef(param_key="watch", value_type="string", default_value=""),
    ]
    ps = ParamSet(defs, {"max_steps": "5"})
    assert ps.int("max_steps") == 5
    assert ps.dec("drop_pct") == Decimal("10")      # 값 없으면 기본값
    assert ps.bool("exclude_etf") is True
    assert ps.time("liquidate_time") == _dt.time(15, 15)
    assert ps.str("watch") == ""
    assert ps.int("없는키", 99) == 99


def test_validate_all_collects_errors():
    defs = [pdef(param_key="a", value_type="int", min_value="0", max_value="10"),
            pdef(param_key="b", value_type="bool")]
    ok, errors = validate_all(defs, {"a": "99", "b": "1"})
    assert errors and ok.get("b") == "1"
    ok2, errors2 = validate_all(defs, {"a": "5", "b": "0"})
    assert not errors2 and ok2 == {"a": "5", "b": "0"}
