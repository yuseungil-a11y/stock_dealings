"""DART 고유번호(corp_code) ↔ 종목코드 매핑 갱신 — 기업 재무분석 전용.

* `GET /api/corpCode.xml` 로 전체 상장/비상장 목록(ZIP, 약 3.6MB / 압축 해제 28MB)을 받아
  **대상 종목(시총 상위, ETF 제외)에 해당하는 것만** `company_corp_code` 에 upsert 한다.
  전체 12만 건을 DB 에 쌓지 않는다.
* DART 고유번호는 거의 바뀌지 않으므로 **월 1회**면 충분하다(`REFRESH_DAYS`).
  단, 대상 종목 중 **아직 매핑이 없는 종목이 새로 생기면** 주기와 무관하게 한 번 더 받는다
  (신규 상장·유니버스 변동 대응). 그 경우에도 **하루 1회를 넘겨 재다운로드하지 않는다**.
* 매매와 무관하다. 이 모듈은 주문·신호·게이트 어느 것도 읽거나 쓰지 않는다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from ..dart.client import DartError
from ..dart.parse import corp_codes_for
from ..util import today_kst

log = logging.getLogger(__name__)

# 고유번호 전체 재확인 주기(일) — 월 1회
REFRESH_DAYS = 30
# 매핑이 빠진 종목이 있어도 하루에 한 번만 다시 내려받는다(28MB 다운로드 폭주 방지)
MIN_DOWNLOAD_INTERVAL_DAYS = 1

# 프로세스 안에서 마지막으로 corpCode.xml 을 내려받은 날짜
_STATE_LOCK = threading.Lock()
_last_download_date: _dt.date | None = None


def reset_state() -> None:
    """마지막 다운로드 날짜 표시를 지운다(테스트/재설정용)."""
    global _last_download_date
    with _STATE_LOCK:
        _last_download_date = None


def last_download_date() -> _dt.date | None:
    with _STATE_LOCK:
        return _last_download_date


def _mark_downloaded(day: _dt.date) -> None:
    global _last_download_date
    with _STATE_LOCK:
        _last_download_date = day


def _age_days(updated_at, today: _dt.date) -> int | None:
    """마지막 갱신 후 경과 일수. 판단할 수 없으면 None."""
    if isinstance(updated_at, _dt.datetime):
        base = updated_at.date()
    elif isinstance(updated_at, _dt.date):
        base = updated_at
    else:
        return None
    return max(0, (today - base).days)


class CorpCodeSync:
    """`company_corp_code` 갱신기."""

    def __init__(self, db, client):
        self.db = db
        self.client = client

    # ------------------------------------------------------------------ #
    def skip_reason(self, target_codes: set[str], today: _dt.date | None = None,
                    force: bool = False) -> str:
        """지금 내려받지 않아도 되는 이유(빈 문자열이면 받아야 한다)."""
        today = today or today_kst()
        if force:
            return ""
        last = last_download_date()
        if last is not None and (today - last).days < MIN_DOWNLOAD_INTERVAL_DAYS:
            return f"오늘({last}) 이미 corpCode.xml 을 내려받음"
        try:
            stats = self.db.company_corp_code_stats() or {}
            known = {str(r["stk_cd"]) for r in
                     (self.db.company_corp_codes(sorted(target_codes)) or [])}
        except Exception:  # noqa: BLE001 - 조회 실패는 '갱신 필요'로 본다(안전한 쪽)
            log.warning("corp_code 매핑 상태 조회 실패 - 갱신을 시도합니다", exc_info=True)
            return ""
        missing = target_codes - known
        if missing:
            return ""          # 대상에 새 종목이 생겼다 → 주기와 무관하게 갱신
        age = _age_days(stats.get("updated_at"), today)
        if age is None or age >= REFRESH_DAYS:
            return ""
        return f"매핑이 {age}일 전 갱신됨(주기 {REFRESH_DAYS}일), 누락 종목 없음"

    # ------------------------------------------------------------------ #
    def sync(self, targets, today: _dt.date | None = None, force: bool = False) -> dict:
        """대상 종목의 corp_code 매핑을 갱신한다.

        `targets` 는 종목코드 iterable. 반환: `{"skipped", "matched", "saved", "missing"}`.
        실패는 예외로 올리지 않고 `error` 에 담는다(엔진을 죽이지 않는다).
        """
        today = today or today_kst()
        wanted = {str(c).strip() for c in targets if str(c or "").strip()}
        out = {"skipped": "", "matched": 0, "saved": 0, "missing": 0, "error": ""}
        if not wanted:
            out["skipped"] = "대상 종목 없음"
            return out

        reason = self.skip_reason(wanted, today, force=force)
        if reason:
            out["skipped"] = reason
            log.debug("corp_code 매핑 갱신 생략: %s", reason)
            return out

        try:
            xml_text = self.client.corp_code_xml()
        except DartError as exc:
            out["error"] = f"corpCode.xml 조회 실패: {exc}"
            log.error("corp_code 매핑 갱신 실패: %s", exc)
            return out
        # 다운로드에 성공한 시점에 '오늘 몫'을 소진한 것으로 본다(파싱/저장이 실패해도
        # 같은 날 28MB 를 다시 받지 않는다)
        _mark_downloaded(today)

        rows = corp_codes_for(xml_text, wanted)
        out["matched"] = len(rows)
        out["missing"] = len(wanted) - len(rows)
        try:
            out["saved"] = self.db.upsert_company_corp_codes(rows)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"매핑 저장 실패: {type(exc).__name__}"
            log.warning("corp_code 매핑 저장 실패", exc_info=True)
            return out
        log.info("DART 고유번호 매핑 갱신: 대상 %d종목 중 %d건 매칭(미매칭 %d)",
                 len(wanted), out["matched"], out["missing"])
        return out
