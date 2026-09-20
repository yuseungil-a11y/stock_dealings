"""키움 응답 파서 단위테스트."""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

import pytest

from stock_svr.kiwoom.parse import (
    hhmmss_to_dt,
    norm_stk_cd,
    rows,
    side_from_code,
    to_date,
    to_datetime,
    to_dec,
    to_int,
)
from stock_svr.util import mask_account_no, mask_secret, mask_text, parse_hhmm


@pytest.mark.parametrize("raw,expected", [
    ("+60700", 60700),
    ("-500", -500),
    ("000012345", 12345),
    ("-000000500", -500),
    ("1,234,567", 1234567),
    ("", None),
    (None, None),
    ("abc", None),
    (42, 42),
])
def test_to_int(raw, expected):
    assert to_int(raw) == expected


def test_to_int_default():
    assert to_int("", 0) == 0
    assert to_int(None, -1) == -1


@pytest.mark.parametrize("raw,expected", [
    ("+3.45", Decimal("3.45")),
    ("-12.5", Decimal("-12.5")),
    ("0", Decimal("0")),
    ("15.00%", Decimal("15.00")),
])
def test_to_dec(raw, expected):
    assert to_dec(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("A005930", "005930"),
    ("005930", "005930"),
    ("005930_NX", "005930"),
    ("005930_AL", "005930"),
    ("a000660", "000660"),
    ("", ""),
    (None, ""),
])
def test_norm_stk_cd(raw, expected):
    assert norm_stk_cd(raw) == expected


def test_to_date():
    assert to_date("20260919") == _dt.date(2026, 9, 19)
    assert to_date("2026-09-19") == _dt.date(2026, 9, 19)
    assert to_date("bad") is None


def test_to_datetime():
    assert to_datetime("20260919153045") == _dt.datetime(2026, 9, 19, 15, 30, 45)
    assert to_datetime("20260919") == _dt.datetime(2026, 9, 19, 0, 0, 0)


def test_hhmmss_to_dt():
    base = _dt.date(2026, 9, 19)
    assert hhmmss_to_dt("153045", base) == _dt.datetime(2026, 9, 19, 15, 30, 45)
    assert hhmmss_to_dt("0930", base) == _dt.datetime(2026, 9, 19, 9, 30, 0)   # HHMM
    assert hhmmss_to_dt("093015", base) == _dt.datetime(2026, 9, 19, 9, 30, 15)
    assert hhmmss_to_dt("", base) is None


def test_side_from_code():
    assert side_from_code("1") == "SELL"
    assert side_from_code("2") == "BUY"
    assert side_from_code("현금매수") == "BUY"
    assert side_from_code("현금매도") == "SELL"
    assert side_from_code("") is None


def test_rows():
    assert rows({"list": [{"a": 1}, "x"]}, "list") == [{"a": 1}]
    assert rows(None, "list") == []
    assert rows({}, "list") == []


def test_parse_hhmm():
    assert parse_hhmm("09:05") == _dt.time(9, 5)
    assert parse_hhmm("0905") == _dt.time(9, 5)
    assert parse_hhmm("25:00") is None
    assert parse_hhmm("") is None


def test_masking():
    assert "***" in mask_text('appkey="abcdef123456"')
    assert "abcdef" not in mask_text('secretkey=abcdef123456')
    assert mask_text("authorization: Bearer abc.def-123") == "authorization: Bearer ***"
    masked = mask_secret("abcdefghijklmn")
    assert masked.startswith("abcd") and "efghijklmn" not in masked
    assert mask_account_no("1234567890") == "******7890"
