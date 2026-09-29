"""거래 종합분석 리포트 (Claude, 참고용) — 웹 요청 → 서버 폴링 처리.

기간(기본 최근 7일, 사용자가 선택 가능)의 `daily_trade_summary`(ka10170 당일매매일지,
종목별 일 손익)를 `v_trade_analysis`(신호→주문→체결→Claude 판단)로 보강해, Claude 가
"이 거래가 왜 이겼는지/졌는지"와 기간 전체의 공통 패턴을 설명하는 참고용 리포트를 만든다.

**매매와 완전히 분리되어 있다** (`company_fundamentals` 와 같은 원칙)
* `algorithm`/`algorithm_selection` 에 등록하지 않는다(알고리즘이 아니다).
* `signal_log`/`orders`/`risk_guard`/주문 게이트 어디에도 쓰지 않는다 - **읽기만** 한다.
* 주문 게이트(`order_enabled`·`real_trading_confirm`·`trading_mode`)를 읽지도 쓰지도 않는다.

큐 패턴 (웹의 "종합분석 요청" 버튼)
* 웹(`stock_web` 계정)은 Anthropic 자격증명이 없어 `trade_analysis_request` 에 `pending`
  행만 INSERT 한다. 서버가 `REQUEST_POLL_SEC` 마다 확인해 **원자적으로 claim** 하고
  처리한다(`TradeAnalysisWorker`) - `trend_scan_request`/`TrendRequestWorker` 와 동일한
  패턴이다. 자동거래·주문 게이트 상태와 무관하게 동작한다.

안전 원칙
* 동시에 **1건만** 처리한다 - 이미 `processing` 인 요청이 있으면 새 pending 을 집지
  않는다(유료 호출 낭비 방지).
* claim 은 `UPDATE ... WHERE id=%s AND status='pending'` 의 영향 행 수로 판정한다.
* `processing` 인 채 `STALE_PROCESSING_MIN` 분 넘게 멈춘 요청은 서버 재시작으로 보고
  `error` 로 정리한다(다음 요청이 영영 막히지 않게).
* `poll_once()`/`_process()` 는 어떤 예외도 밖으로 던지지 않는다(엔진 루프가 죽지 않게).
* 조회 기간에 오늘 날짜가 포함되면, 당일매매일지가 장마감 정리(보통 16시 이후) 전에는
  비어 있거나 불완전할 수 있다는 사실을 프롬프트에 명시해 리포트가 이를 언급하게 한다.
"""
from __future__ import annotations

import datetime as _dt
import logging

from ..llm.client import MODELS, ClaudeError
from ..llm.trade_analysis_prompt import (
    REPORT_MAX_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    TRADE_ANALYSIS_OUTPUT_SCHEMA,
    build_report_prompt,
    build_trade_payload,
    validate_trade_analysis_output,
)
from ..llm.prompt import OutputSchemaError
from ..util import mask_text, now_kst

log = logging.getLogger(__name__)

# 로그·이벤트에서 이 기능을 가리키는 이름. **`algorithm.code` 가 아니다**(DB 에 등록하지 않는다).
CODE = "trade_analysis"

# 엔진 하트비트(10초)와 비슷한 짧은 주기로 확인한다. 자동거래·주문 게이트 상태와
# **무관하게** 엔진이 돌고 있으면 항상 동작한다 - trend_scan_request 와 동일한 주기.
REQUEST_POLL_SEC = 20
# `processing` 인 채 이만큼 지난 요청은 서버가 중간에 죽은 것으로 보고 error 로 정리한다
STALE_PROCESSING_MIN = 10
STALE_REASON = "서버 재시작으로 중단"

# claude_trend_scan 과 같은 이유(연구·분석형 작업)로 opus 를 기본으로 쓴다.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_TIMEOUT_SEC = 120.0

STATUS_OK = "ok"
STATUS_ERROR = "error"


class NoTradesError(RuntimeError):
    """조회 기간에 `daily_trade_summary` 행이 하나도 없음 - Claude 를 호출하지 않는다."""


def _num(value):
    """Decimal 등 JSON 직렬화가 안 되는 값을 평범한 숫자로."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_date_str(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


class TradeAnalysisResult:
    """분석 1회 결과. 요청 처리기와 CLI 점검이 함께 쓴다."""

    def __init__(self, account_id: int, period_start: _dt.date, period_end: _dt.date,
                requested_by: str = "", model: str = ""):
        self.account_id = account_id
        self.period_start = period_start
        self.period_end = period_end
        self.requested_by = requested_by
        self.status = STATUS_OK
        self.model = model
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.total_pl_amt = 0
        self.win_rate: float | None = None
        self.summary = ""
        self.report_text = ""
        self.input_tokens = 0
        self.output_tokens = 0
        self.error_msg = ""
        # False = 거래 기록이 없어 Claude 를 호출하지 않았다(리포트 행도 만들지 않는다)
        self.attempted = True


class TradeAnalysisService:
    """`daily_trade_summary`+`v_trade_analysis` → Claude 리포트 1건. 신호·주문은 만들지 않는다."""

    def __init__(self, db, anthropic_cfg=None, client=None, model: str = DEFAULT_MODEL,
                timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self.db = db
        self.anthropic_cfg = anthropic_cfg
        self._client = client
        self.model = model if model in MODELS else DEFAULT_MODEL
        self.timeout_sec = max(30.0, float(timeout_sec))

    def claude(self):
        if self._client is None:
            from ..algo.claude_advisor import get_client

            if self.anthropic_cfg is None:
                raise ClaudeError("Anthropic 설정이 없습니다([anthropic] apikey_file)",
                                  infra=False)
            self._client = get_client(self.anthropic_cfg)
        return self._client

    # ------------------------------------------------------------------ #
    def build_trades(self, account_id: int, start: _dt.date,
                     end: _dt.date) -> tuple[list[dict], dict]:
        """종목+일 단위 거래 목록과 집계(승/패/손익/승률)를 만든다.

        1차 근거는 `daily_trade_summary`(키움 공식 손익)이고, `v_trade_analysis`
        (신호→주문→체결→Claude 판단)로 "왜 그 거래가 있었는지"의 정성적 맥락을 보강한다.
        """
        rows = self.db.daily_trade_summary_range(account_id, start, end) or []
        empty_agg = {"trade_count": 0, "win_count": 0, "loss_count": 0,
                    "total_pl_amt": 0, "win_rate": None}
        if not rows:
            return [], empty_agg

        codes = sorted({str(r["stk_cd"]) for r in rows if r.get("stk_cd")})
        try:
            ctx_rows = self.db.trade_analysis_context(start, end, codes) if codes else []
        except Exception:  # noqa: BLE001 - 맥락 보강 실패는 손익 집계 자체를 막지 않는다
            log.warning("%s: 거래 맥락(v_trade_analysis) 조회 실패 - 맥락 없이 진행", CODE,
                        exc_info=True)
            ctx_rows = []

        # (종목, 날짜) 당 가장 나중 신호/주문을 그 날의 대표 맥락으로 쓴다(여러 건이면 마지막 것).
        ctx_index: dict[tuple[str, str], dict] = {}
        for c in ctx_rows:
            code = str(c.get("stk_cd") or "")
            when = c.get("order_time") or c.get("signal_time")
            if not code or when is None:
                continue
            ctx_index[(code, _as_date_str(when))] = c

        trades: list[dict] = []
        pl_values: list[int] = []
        for r in rows:
            code = str(r.get("stk_cd") or "")
            day = _as_date_str(r.get("base_dt"))
            c = ctx_index.get((code, day), {})
            pl = int(r.get("pl_amt") or 0)
            pl_values.append(pl)
            trades.append({
                "date": day, "stk_cd": code, "stk_nm": r.get("stk_nm"),
                "buy_qty": r.get("buy_qty"), "buy_avg_pric": r.get("buy_avg_pric"),
                "sell_qty": r.get("sell_qty"), "sell_avg_pric": r.get("sell_avg_pric"),
                "pl_amt": pl, "prft_rt": _num(r.get("prft_rt")),
                "entry_algo": c.get("algo_code"),
                "entry_reason": c.get("signal_detail") or c.get("order_reason"),
                "claude_reviewed": bool(c.get("llm_decision")),
                "claude_decision": c.get("llm_decision"),
                "claude_reasons": c.get("llm_reasons"),
                "slippage_pct": _num(c.get("slippage_pct")),
            })

        win_count = sum(1 for v in pl_values if v > 0)
        loss_count = sum(1 for v in pl_values if v < 0)
        decided = win_count + loss_count
        aggregate = {
            "trade_count": len(trades), "win_count": win_count, "loss_count": loss_count,
            "total_pl_amt": sum(pl_values),
            "win_rate": round(win_count / decided * 100, 2) if decided else None,
        }
        return trades, aggregate

    # ------------------------------------------------------------------ #
    def run(self, account_id: int, period_start: _dt.date, period_end: _dt.date,
           requested_by: str = "") -> TradeAnalysisResult:
        """분석 1회. 예외를 밖으로 던지지 않고 status='error' 로 기록한다."""
        result = TradeAnalysisResult(account_id, period_start, period_end, requested_by,
                                     model=self.model)
        try:
            self._run(result)
        except NoTradesError as exc:
            result.attempted = False
            result.status = STATUS_ERROR
            result.error_msg = str(exc)
            log.info("%s: %s (계좌=%s, 기간=%s~%s)", CODE, result.error_msg, account_id,
                     period_start, period_end)
        except ClaudeError as exc:
            result.status = STATUS_ERROR
            result.error_msg = f"Claude 오류: {exc}"
            log.error("%s: %s", CODE, result.error_msg)
        except Exception as exc:  # noqa: BLE001 - 분석 실패가 엔진을 죽이지 않게
            result.status = STATUS_ERROR
            result.error_msg = f"{type(exc).__name__}: {mask_text(str(exc))[:150]}"
            log.exception("%s 실패", CODE)
        return result

    def _run(self, result: TradeAnalysisResult) -> None:
        trades, aggregate = self.build_trades(result.account_id, result.period_start,
                                              result.period_end)
        result.trade_count = aggregate["trade_count"]
        result.win_count = aggregate["win_count"]
        result.loss_count = aggregate["loss_count"]
        result.total_pl_amt = aggregate["total_pl_amt"]
        result.win_rate = aggregate["win_rate"]
        if not trades:
            raise NoTradesError("해당 기간에 매매 기록이 없습니다(daily_trade_summary 비어있음)")

        today_incomplete = result.period_end >= now_kst().date()
        payload = build_trade_payload(period_start=result.period_start,
                                      period_end=result.period_end, trades=trades,
                                      aggregate=aggregate, today_incomplete=today_incomplete)
        try:
            res = self.claude().extract(
                build_report_prompt(payload), model=self.model, system=SYSTEM_PROMPT,
                schema=TRADE_ANALYSIS_OUTPUT_SCHEMA, timeout_sec=self.timeout_sec,
                max_tokens=REPORT_MAX_OUTPUT_TOKENS)
            parsed = validate_trade_analysis_output(res.data)
        except OutputSchemaError as exc:
            raise ClaudeError(f"응답 스키마 위반: {exc}", infra=False) from None
        result.summary = parsed["summary"]
        result.report_text = parsed["report_text"]
        result.input_tokens = res.input_tokens
        result.output_tokens = res.output_tokens
        result.model = res.model or self.model


class TradeAnalysisWorker:
    """웹의 "종합분석 요청"(`trade_analysis_request`) 처리기.

    엔진 루프가 `REQUEST_POLL_SEC`(기본 20초)마다 `poll_once()` 를 부른다.
    **자동거래 ON/OFF·주문 게이트와 무관하게** 엔진이 돌고 있으면 항상 동작한다
    (웹은 Anthropic 자격증명이 없어 직접 실행할 수 없다).
    """

    def __init__(self, db, account_id: int | None = None, anthropic_cfg=None, client=None,
                model: str = DEFAULT_MODEL, timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self.db = db
        self.account_id = account_id
        self.anthropic_cfg = anthropic_cfg
        self.client = client
        self.model = model
        self.timeout_sec = timeout_sec
        self.processed = 0

    # ------------------------------------------------------------------ #
    def cleanup_stale(self, minutes: int = STALE_PROCESSING_MIN) -> int:
        """`processing` 인 채 멈춘 요청을 정리한다(서버가 처리 중 죽은 경우)."""
        n = self.db.expire_stale_trade_analysis_requests(minutes, STALE_REASON)
        if n:
            log.warning("%s: 멈춰 있던 종합분석 요청 %d건을 오류로 정리했습니다(%d분 경과)",
                        CODE, n, minutes)
            try:
                self.db.log_event("WARN", "algo",
                                  f"거래 종합분석 요청 {n}건 정리 ({STALE_REASON})")
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
            if self.db.count_processing_trade_analysis_requests() > 0:
                log.debug("%s: 이미 처리 중인 종합분석 요청이 있어 건너뜁니다", CODE)
                return None
            req = self.db.claim_trade_analysis_request()
        except Exception:  # noqa: BLE001 - 조회 실패는 다음 주기에 재시도
            log.warning("%s: 종합분석 요청 조회 실패", CODE, exc_info=True)
            return None
        if req is None:
            return None
        return self._process(req)

    # ------------------------------------------------------------------ #
    def _process(self, req: dict) -> dict:
        req_id = int(req["id"])
        account_id = int(req["account_id"])
        period_start = req["period_start"]
        period_end = req["period_end"]
        who = str(req.get("requested_by") or "")[:50]
        out = {"request_id": req_id, "requested_by": who, "status": STATUS_ERROR,
               "report_id": None, "trade_count": 0, "error": ""}
        log.warning("%s: 종합분석 요청 처리 시작 (id=%d, 계좌=%s, 기간=%s~%s, 요청자=%s)",
                    CODE, req_id, account_id, period_start, period_end, who or "-")
        try:
            svc = TradeAnalysisService(self.db, anthropic_cfg=self.anthropic_cfg,
                                       client=self.client, model=self.model,
                                       timeout_sec=self.timeout_sec)
            result = svc.run(account_id, period_start, period_end, requested_by=who)
            report_id = None
            if result.attempted:
                report_id = self.db.insert_trade_analysis_report(
                    account_id, period_start, period_end, requested_by=who or "-",
                    model=result.model, trade_count=result.trade_count,
                    win_count=result.win_count, loss_count=result.loss_count,
                    total_pl_amt=result.total_pl_amt, win_rate=result.win_rate,
                    summary=result.summary or None,
                    report_text=result.report_text or "(리포트 생성 실패)",
                    input_tokens=result.input_tokens or None,
                    output_tokens=result.output_tokens or None,
                    status=result.status, error_msg=result.error_msg or None)
            self._finish(req_id, "error" if result.status == STATUS_ERROR else "done",
                        report_id=report_id, error_msg=result.error_msg or None)
            out.update(status=result.status, report_id=report_id,
                       trade_count=result.trade_count, error=result.error_msg)
            self.processed += 1
            log.warning("%s: 종합분석 완료 (id=%d, status=%s, 거래 %d건, report_id=%s)",
                        CODE, req_id, result.status, result.trade_count, report_id)
            try:
                self.db.log_event(
                    "ERROR" if result.status == STATUS_ERROR else "INFO", "algo",
                    f"거래 종합분석({who or '-'}) {result.status}: 거래 {result.trade_count}건"
                    " (참고용 - 매매 신호/주문과 무관)"
                    + (f" | {result.error_msg}" if result.error_msg else ""))
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 요청 1건의 실패가 루프를 죽이지 않게
            msg = f"{type(exc).__name__}: {mask_text(str(exc))[:150]}"
            out["error"] = msg
            log.exception("%s: 종합분석 요청 처리 실패 (id=%d)", CODE, req_id)
            self._finish(req_id, "error", error_msg=msg)
        return out

    def _finish(self, req_id: int, status: str, report_id: int | None = None,
               error_msg: str | None = None) -> None:
        try:
            self.db.finish_trade_analysis_request(req_id, status=status, report_id=report_id,
                                                  error_msg=error_msg)
        except Exception:  # noqa: BLE001 - 상태 갱신 실패는 stale 정리가 나중에 풀어 준다
            log.warning("%s: 종합분석 요청 상태 갱신 실패 (id=%d)", CODE, req_id, exc_info=True)
