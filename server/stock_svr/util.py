"""공용 유틸: KST 시각, 장 운영시간 판단, 비밀값 마스킹."""
from __future__ import annotations

import datetime as _dt
import re

try:  # Windows 에 tzdata 가 없을 수 있으므로 방어
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # pragma: no cover - 환경 의존
    KST = _dt.timezone(_dt.timedelta(hours=9))

MARKET_OPEN = _dt.time(9, 0)
MARKET_CLOSE = _dt.time(15, 30)


def now_kst() -> _dt.datetime:
    """DB 가 KST naive DATETIME 을 쓰므로 tz 를 제거한 KST 시각을 돌려준다."""
    return _dt.datetime.now(KST).replace(tzinfo=None)


def today_kst() -> _dt.date:
    return now_kst().date()


def is_weekday(d: _dt.date) -> bool:
    return d.weekday() < 5


def is_market_open(now: _dt.datetime | None = None) -> bool:
    """평일 09:00~15:30 KST 이면 True. (공휴일은 WS `0s`/API 응답으로 별도 보정)"""
    now = now or now_kst()
    if not is_weekday(now.date()):
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def parse_hhmm(text: str | None) -> _dt.time | None:
    """'09:05' / '0905' → time. 빈 값이면 None."""
    if not text:
        return None
    s = str(text).strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2}):?(\d{2})", s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return _dt.time(hh, mm)


_KV_SECRET_RE = re.compile(
    r"(?i)(appkey|secretkey|secret|password|passwd|pwd|token|authorization|api[_-]?key)"
    r"(\"?\s*[:=]\s*\"?)(?!Bearer\b|\*)([^\s\",;&}]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+(?!\*)([A-Za-z0-9._\-]+)")
# Anthropic API 키는 값만 있어도 알아볼 수 있으므로 패턴 자체를 지운다(예외 메시지 유출 방지)
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9._\-]+")
# 계좌번호(10~12자리)는 '계좌' 류 라벨이 붙은 경우에만 지운다 (R-18).
# 라벨 없이 숫자만 보고 지우면 주문번호·금액까지 훼손되므로 범위를 좁힌다.
_ACCOUNT_NO_RE = re.compile(
    r"(?i)(계좌번호|계좌|acct[_-]?no|account[_-]?no|acctNo)(\"?\s*[:=]?\s*\"?)(\d{10,12})")


def mask_secret(value: str | None, keep: int = 4) -> str:
    """비밀값 자체를 마스킹. 앞 keep 자리만 남긴다."""
    if not value:
        return "(none)"
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * (len(s) - keep) + f"(len={len(s)})"


def mask_text(text: str) -> str:
    """로그 문자열 안의 key=value 형태 비밀값을 마스킹."""
    if not text:
        return text
    out = _ANTHROPIC_KEY_RE.sub("sk-ant-***", text)
    out = _BEARER_RE.sub("Bearer ***", out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", out)
    out = _ACCOUNT_NO_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{mask_account_no(m.group(3))}", out)
    return out


def mask_account_no(acct: str | None) -> str:
    """계좌번호는 끝 4자리만 표시."""
    if not acct:
        return "(none)"
    s = str(acct)
    if len(s) <= 4:
        return s
    return "*" * (len(s) - 4) + s[-4:]
