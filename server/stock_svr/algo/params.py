"""`algorithm_param_def` 기반 파라미터 검증/변환.

UI 는 이 모듈로 입력값을 검증하고, 알고리즘은 `ParamSet` 으로 타입 변환된 값을 읽는다.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from ..util import parse_hhmm

TRUE_SET = {"1", "true", "t", "y", "yes", "on"}
FALSE_SET = {"0", "false", "f", "n", "no", "off"}


class ParamError(ValueError):
    """파라미터 검증 실패."""


def enum_choices(pdef: dict) -> list[tuple[str, str]]:
    """'value:라벨,value:라벨' → [(value, label), ...]"""
    raw = (pdef.get("enum_options") or "").strip()
    out: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            v, lab = chunk.split(":", 1)
            out.append((v.strip(), lab.strip()))
        else:
            out.append((chunk, chunk))
    return out


def validate(pdef: dict, raw) -> str:
    """정의에 맞는지 검사하고 **저장용 문자열**을 돌려준다. 실패 시 ParamError."""
    vtype = (pdef.get("value_type") or "string").lower()
    label = pdef.get("label") or pdef.get("param_key") or "?"
    s = "" if raw is None else str(raw).strip()

    if vtype == "bool":
        low = s.lower()
        if low in TRUE_SET:
            return "1"
        if low in FALSE_SET:
            return "0"
        raise ParamError(f"{label}: 참/거짓 값이어야 합니다.")

    if vtype == "enum":
        choices = [v for v, _ in enum_choices(pdef)]
        if choices and s not in choices:
            raise ParamError(f"{label}: 허용된 값이 아닙니다 ({', '.join(choices)})")
        return s

    if vtype == "time":
        if s == "":
            # 빈 값 허용(예: 청산 시각 미사용)
            return ""
        if parse_hhmm(s) is None:
            raise ParamError(f"{label}: HH:MM 형식이어야 합니다.")
        t = parse_hhmm(s)
        return f"{t.hour:02d}:{t.minute:02d}"

    if vtype == "int":
        try:
            raw_dec = Decimal(s)
        except (InvalidOperation, ValueError):
            raise ParamError(f"{label}: 정수를 입력하세요.") from None
        if not raw_dec.is_finite():          # NaN / Infinity 거부 (S-19)
            raise ParamError(f"{label}: 유한한 숫자를 입력하세요.")
        val = int(raw_dec)
        _range_check(pdef, Decimal(val), label)
        return str(val)

    if vtype == "decimal":
        try:
            val = Decimal(s)
        except InvalidOperation:
            raise ParamError(f"{label}: 숫자를 입력하세요.") from None
        if not val.is_finite():              # NaN / Infinity 거부 (S-19)
            raise ParamError(f"{label}: 유한한 숫자를 입력하세요.")
        _range_check(pdef, val, label)
        return _fmt_decimal(val)

    # string
    if len(s) > 100:
        raise ParamError(f"{label}: 100자 이내로 입력하세요.")
    return s


def _fmt_decimal(val: Decimal) -> str:
    s = format(val, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _range_check(pdef: dict, val: Decimal, label: str) -> None:
    mn, mx = pdef.get("min_value"), pdef.get("max_value")
    if mn not in (None, ""):
        try:
            if val < Decimal(str(mn)):
                raise ParamError(f"{label}: 최소 {mn} 이상이어야 합니다.")
        except InvalidOperation:
            pass
    if mx not in (None, ""):
        try:
            if val > Decimal(str(mx)):
                raise ParamError(f"{label}: 최대 {mx} 이하여야 합니다.")
        except InvalidOperation:
            pass


def coerce(pdef: dict, raw):
    """저장된 문자열을 파이썬 값으로."""
    vtype = (pdef.get("value_type") or "string").lower()
    s = "" if raw is None else str(raw).strip()
    if vtype == "bool":
        return s.lower() in TRUE_SET
    if vtype == "int":
        try:
            return int(Decimal(s))
        except (InvalidOperation, ValueError):
            return int(Decimal(str(pdef.get("default_value") or 0)))
    if vtype == "decimal":
        try:
            return Decimal(s)
        except InvalidOperation:
            return Decimal(str(pdef.get("default_value") or 0))
    if vtype == "time":
        return parse_hhmm(s)
    return s


class ParamSet:
    """알고리즘이 쓰는 타입 변환된 파라미터 맵."""

    def __init__(self, param_defs: list[dict], values: dict[str, str] | None = None):
        self._defs = {d["param_key"]: d for d in param_defs}
        self._raw = dict(values or {})
        for key, d in self._defs.items():
            self._raw.setdefault(key, d.get("default_value"))
        # S-16: 저장된 값이 정의(타입·min/max)를 벗어나면 기록해 둔다.
        # 레지스트리가 이를 보고 해당 알고리즘을 비활성화한다.
        self.invalid: list[str] = []
        for key, d in self._defs.items():
            try:
                validate(d, self._raw.get(key))
            except ParamError as exc:
                self.invalid.append(str(exc))

    def __contains__(self, key: str) -> bool:
        return key in self._defs

    def raw(self, key: str, default=None):
        return self._raw.get(key, default)

    def get(self, key: str, default=None):
        d = self._defs.get(key)
        if d is None:
            return default
        return coerce(d, self._raw.get(key))

    def int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def dec(self, key: str, default: float = 0.0) -> Decimal:
        v = self.get(key)
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal(str(default))

    def float(self, key: str, default: float = 0.0) -> float:
        return float(self.dec(key, default))

    def bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        return bool(v) if v is not None else default

    def str(self, key: str, default: str = "") -> str:
        v = self.get(key)
        return default if v is None else str(v)

    def time(self, key: str) -> _dt.time | None:
        v = self.get(key)
        return v if isinstance(v, _dt.time) else None

    def as_dict(self) -> dict:
        return {k: self.get(k) for k in self._defs}


def validate_all(param_defs: list[dict], raw_values: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """전부 검증. (정규화된 값, 오류 메시지 목록)"""
    out: dict[str, str] = {}
    errors: list[str] = []
    for d in param_defs:
        key = d["param_key"]
        if key not in raw_values:
            continue
        try:
            out[key] = validate(d, raw_values[key])
        except ParamError as exc:
            errors.append(str(exc))
    return out, errors
