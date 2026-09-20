"""키움 API 응답 값 파서.

API 는 숫자를 부호/0패딩 문자열로 돌려준다(`"+60700"`, `"-500"`, `"000012345"`).
종목코드에는 `A` 접두나 `_NX`/`_AL` 접미가 붙을 수 있다.
"""
from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation

_NUM_RE = re.compile(r"^[+-]?[0-9]*\.?[0-9]*$")


def to_int(value, default: int | None = None) -> int | None:
    """'+60700' -> 60700, '-000500' -> -500, '' -> default."""
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, Decimal):
        return int(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return default
    if not _NUM_RE.match(s):
        return default
    try:
        return int(Decimal(s))
    except (InvalidOperation, ValueError):
        return default


def to_dec(value, default: Decimal | None = None) -> Decimal | None:
    """'+3.45' -> Decimal('3.45'). 비율 문자열용."""
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return default
    if not _NUM_RE.match(s):
        return default
    try:
        return Decimal(s)
    except InvalidOperation:
        return default


def to_float(value, default: float | None = None) -> float | None:
    d = to_dec(value, None)
    return float(d) if d is not None else default


def norm_stk_cd(code) -> str:
    """'A005930', '005930_NX', '005930_AL' -> '005930'."""
    if code is None:
        return ""
    s = str(code).strip().upper()
    if not s:
        return ""
    for suffix in ("_NX", "_AL"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    if len(s) > 6 and s.startswith("A") and s[1:].isdigit():
        s = s[1:]
    return s


def to_date(value) -> _dt.date | None:
    """'20260919' / '2026-09-19' -> date."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    s = re.sub(r"[^0-9]", "", s)
    if len(s) < 8:
        return None
    try:
        return _dt.date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def to_datetime(value) -> _dt.datetime | None:
    """'20260919153045' / '20260919' -> datetime."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value
    s = re.sub(r"[^0-9]", "", str(value).strip())
    if len(s) >= 14:
        try:
            return _dt.datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    if len(s) >= 8:
        d = to_date(s[:8])
        return _dt.datetime.combine(d, _dt.time()) if d else None
    return None


def hhmmss_to_dt(value, base_date: _dt.date | None = None) -> _dt.datetime | None:
    """WS 의 '153045' / '1530' 같은 시각 필드를 오늘 날짜 기준 datetime 으로."""
    if value is None:
        return None
    s = re.sub(r"[^0-9]", "", str(value).strip())
    if not s:
        return None
    if len(s) >= 14:
        return to_datetime(s)
    if len(s) == 4:          # HHMM
        s += "00"
    s = s.zfill(6)[:6]
    try:
        t = _dt.time(int(s[0:2]), int(s[2:4]), int(s[4:6]))
    except ValueError:
        return None
    base = base_date or _dt.date.today()
    return _dt.datetime.combine(base, t)


def side_from_code(value) -> str | None:
    """WS 907(매도수구분) / REST io_tp_nm 등에서 BUY/SELL 판별."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in ("1", "01"):
        return "SELL"
    if s in ("2", "02"):
        return "BUY"
    if "매수" in s:
        return "BUY"
    if "매도" in s:
        return "SELL"
    return None


def rows(resp: dict | None, key: str) -> list[dict]:
    """응답에서 리스트 필드를 안전하게 꺼낸다."""
    if not resp:
        return []
    val = resp.get(key)
    if isinstance(val, list):
        return [r for r in val if isinstance(r, dict)]
    return []
