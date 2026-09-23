"""온디맨드 재무데이터 수집 (fundamentals_filter 전용) — 종목 1개만, 신호는 만들지 않는다.

배경
* `fundamentals_filter`(매매 사이클 안에서 도는 필터)는 `company_valuation_daily` 를
  읽기만 하는 **순수 DB 읽기 함수**라 빨라야 한다. 재무데이터가 없거나 오래된 종목을
  만나도 **그 자리에서 DART 를 호출하지 않는다** — 매매 사이클 안에서 절대 동기적으로
  네트워크 호출을 하면 안 된다는 원칙을 그대로 지킨다.
* 대신 `trend_scan_request`(웹의 "지금 다시 조사" 요청 큐)와 동일한 패턴으로,
  `company_fetch_request` 테이블에 종목코드만 빠르게 INSERT 하고(`enqueue()`),
  엔진이 백그라운드에서 `FETCH_REQUEST_POLL_SEC` 마다 폴링해(`FetchRequestWorker`)
  그 종목 1개만 재수집한다.
* **Claude 리포트는 만들지 않는다** — 필터가 필요한 건 숫자뿐이고 비용이 들면 안 된다
  (`FundamentalsService.fetch_one()` 은 corp_code 매핑 + 재무제표 수집 + 밸류에이션
  계산만 하고, `make_reports()` 는 절대 호출하지 않는다).
* 게이트(order_enabled/real_trading_confirm/trading_mode)·algorithm/algorithm_selection/
  signal_log/orders/Executor/risk_guard 어디에도 연결되지 않는다(기업 재무분석 기능과
  동일한 매매-분리 원칙).
"""
from __future__ import annotations

import logging

from ..util import mask_text

log = logging.getLogger(__name__)

CODE = "fundamentals_fetch"

# 엔진 루프가 이 큐를 확인하는 주기(초). 웹의 "지금 다시 조사"(trend_scan.REQUEST_POLL_SEC=20)
# 와 비슷한 짧은 주기다 — 자동거래 ON/OFF·주문 게이트와 **무관하게** 엔진이 돌고 있으면 항상
# 동작한다.
FETCH_REQUEST_POLL_SEC = 30

# `processing` 인 채 이만큼(분) 지난 요청은 서버가 중간에 죽은 것으로 보고 error 로 정리한다.
STALE_PROCESSING_MIN = 10
STALE_REASON = "서버 재시작으로 중단"

# 같은 종목이 최근 이만큼(분) 안에 이미 처리됐으면(성공/실패 불문) 다시 큐에 넣지 않는다.
# 하루 안에 같은 종목이 여러 번 차단돼도 DART 를 반복 호출하지 않기 위한 쿨다운이다.
COOLDOWN_MIN = 60

# 종목 1개 온디맨드 수집의 DART 호출 상한. 배치(`FundamentalsOptions.max_fetch`, 기본 300)와
# 무관하게 이 경로는 항상 이 값을 쓴다 — 5년치 분기(약 20개) x 연결/개별(2) 기준으로 여유있게.
MAX_FETCH_PER_REQUEST = 60


# ---------------------------------------------------------------------- #
def enqueue(db, stk_cd: str, stk_nm: str | None = None,
           source: str = "fundamentals_filter") -> bool:
    """온디맨드 재수집 요청을 큐에 넣는다. **DART 를 여기서 직접 호출하지 않는다**
    (dedupe 조회 + INSERT 뿐인 빠른 DB 접근이다).

    dedupe: 같은 종목으로 이미 `pending`/`processing` 요청이 있으면 넣지 않고,
    최근 `COOLDOWN_MIN` 분 안에 처리(성공/실패 불문)된 이력이 있어도 넣지 않는다.
    예외는 여기서 모두 삼킨다 — 호출부(필터)의 차단 판정은 이 함수의 성패와 무관하게
    항상 그대로 반환돼야 한다.
    """
    stk_cd = str(stk_cd or "").strip()
    if not stk_cd:
        return False
    try:
        if db.open_fetch_request(stk_cd):
            log.debug("%s: 이미 처리 대기/중인 재수집 요청이 있어 건너뜁니다 (%s)", CODE, stk_cd)
            return False
        if db.recent_fetch_request(stk_cd, COOLDOWN_MIN):
            log.debug("%s: 최근 %d분 안에 이미 처리된 요청이 있어 건너뜁니다 (%s)",
                     CODE, COOLDOWN_MIN, stk_cd)
            return False
        db.insert_fetch_request(stk_cd, stk_nm, source)
        log.info("%s: 온디맨드 재수집 요청 등록 (%s %s, 출처=%s)", CODE, stk_cd, stk_nm or "-", source)
        return True
    except Exception:  # noqa: BLE001 - 호출부(필터)는 절대 이 실패로 죽으면 안 된다
        log.warning("%s: 재수집 요청 등록 실패 (%s)", CODE, stk_cd, exc_info=True)
        return False


# ---------------------------------------------------------------------- #
class FetchRequestWorker:
    """`company_fetch_request` 큐 처리기.

    엔진 루프가 `FETCH_REQUEST_POLL_SEC`(기본 30초)마다 `poll_once()` 를 부른다.
    **자동거래 ON/OFF·주문 게이트와 무관하게** 엔진이 돌고 있으면 항상 동작한다
    (`trend_scan.TrendRequestWorker` 와 동일한 설계).

    안전 원칙
    * 동시에 **1건만** 처리한다 — 이미 `processing` 인 요청이 있으면 새 pending 을
      집지 않는다(DART 호출 폭주 방지).
    * claim 은 `UPDATE ... WHERE id=%s AND status='pending'` 의 영향 행 수로 판정한다
      (여러 프로세스/스레드가 경쟁해도 한 곳만 이긴다).
    * `FundamentalsService.fetch_one()` 만 호출한다 — Claude 리포트(`make_reports()`)
      는 어떤 경우에도 호출하지 않는다.
    * 어떤 예외도 밖으로 던지지 않는다(엔진 루프가 죽지 않게).
    """

    def __init__(self, db, fundamentals_service):
        self.db = db
        self.fundamentals = fundamentals_service
        self.processed = 0

    # ------------------------------------------------------------------ #
    def cleanup_stale(self, minutes: int = STALE_PROCESSING_MIN) -> int:
        """`processing` 인 채 멈춘 요청을 정리한다(서버가 처리 중 죽은 경우)."""
        n = self.db.expire_stale_fetch_requests(minutes, STALE_REASON)
        if n:
            log.warning("%s: 멈춰 있던 온디맨드 재수집 요청 %d건을 오류로 정리했습니다(%d분 경과)",
                        CODE, n, minutes)
            try:
                self.db.log_event("WARN", "algo",
                                  f"온디맨드 재무데이터 재수집 요청 {n}건 정리 ({STALE_REASON})")
            except Exception:  # noqa: BLE001
                pass
        return n

    def poll_once(self) -> dict | None:
        """pending 요청 1건을 claim 해 처리한다. 처리한 게 없으면 None."""
        try:
            self.cleanup_stale()
        except Exception:  # noqa: BLE001 - 정리 실패가 처리를 막지 않게
            log.debug("%s: 멈춘 요청 정리 실패", CODE, exc_info=True)
        try:
            if self.db.count_processing_fetch_requests() > 0:
                log.debug("%s: 이미 처리 중인 재수집 요청이 있어 건너뜁니다", CODE)
                return None
            req = self.db.claim_fetch_request()
        except Exception:  # noqa: BLE001 - 조회 실패는 다음 주기에 재시도
            log.warning("%s: 재수집 요청 조회 실패", CODE, exc_info=True)
            return None
        if req is None:
            return None
        return self._process(req)

    # ------------------------------------------------------------------ #
    def _process(self, req: dict) -> dict:
        req_id = int(req["id"])
        stk_cd = str(req.get("stk_cd") or "")
        stk_nm = req.get("stk_nm")
        out = {"request_id": req_id, "stk_cd": stk_cd, "status": "error", "error": ""}
        log.info("%s: 온디맨드 재수집 시작 (id=%d, %s %s)", CODE, req_id, stk_cd, stk_nm or "-")
        try:
            result = self.fundamentals.fetch_one(stk_cd, stk_nm, max_fetch=MAX_FETCH_PER_REQUEST)
        except Exception as exc:  # noqa: BLE001 - 요청 1건의 실패가 루프를 죽이지 않게
            msg = f"{type(exc).__name__}: {mask_text(str(exc))[:150]}"
            out["error"] = msg
            log.exception("%s: 온디맨드 재수집 실패 (id=%d, %s)", CODE, req_id, stk_cd)
            self._finish(req_id, "error", msg)
            return out

        errors = [str(e) for e in (result.get("errors") or [])]
        status = "error" if errors else "done"
        error_msg = "; ".join(errors)[:255] if errors else None
        out.update(status=status, error=error_msg or "")
        self.processed += 1
        val = result.get("valuation") or {}
        log.info("%s: 온디맨드 재수집 완료 (id=%d, %s, status=%s%s)", CODE, req_id, stk_cd, status,
                 f", per={val.get('per')}" if val else "")
        try:
            self.db.log_event(
                "WARN" if status == "error" else "INFO", "algo",
                f"온디맨드 재무데이터 재수집({stk_cd} {stk_nm or ''}) {status}"
                + (f" | {error_msg}" if error_msg else ""))
        except Exception:  # noqa: BLE001
            pass
        self._finish(req_id, status, error_msg)
        return out

    def _finish(self, req_id: int, status: str, error_msg: str | None) -> None:
        try:
            self.db.finish_fetch_request(req_id, status=status, error_msg=error_msg)
        except Exception:  # noqa: BLE001 - 상태 갱신 실패는 stale 정리가 나중에 풀어 준다
            log.warning("%s: 재수집 요청 상태 갱신 실패 (id=%d)", CODE, req_id, exc_info=True)
