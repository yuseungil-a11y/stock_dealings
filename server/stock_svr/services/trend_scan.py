"""산업 트렌드 스캔 (claude_trend_scan) — 하루 1회 실행 서비스.

흐름 (하루 1회, `scan_time` 이후 첫 평가 사이클):

    ka90001 테마그룹(읽기 전용) → ① Claude 웹 검색 조사(자유 텍스트)
                               → ② Claude 구조화 추출(JSON, 로컬 재검증)
                               → ③ **종목 확정은 키움/종목마스터로만**
                               → 매수 신호(BUY) → (매매 시간까지 대기) → 기존 파이프라인
                                 (필터→risk_guard→claude_advisor→Executor 게이트)

조사 시각(기본 08:30)은 장 시작 전이라 그때 만든 신호를 바로 넘기면 risk_guard 의
'장 시간이 아님 / 매매시간 외'에 전부 막힌다. 그래서 신호는 대기열에 두었다가
**장이 열리고 risk_guard 의 매매 시간에 들어섰을 때 그날 한 번만** 투입한다
(같은 신호를 매 주기 다시 내보내면 중복 주문 위험이 있다).

안전 원칙
* **모델이 준 종목코드는 쓰지 않는다** — 애초에 요청하지도 않는다(출력 스키마에 코드 필드 없음).
  확정 경로는 ① ka90001 테마명 **정확 일치** → ka90002 구성종목, ② `stock_master.stk_nm`
  **정확 일치** 둘뿐이고, 어느 쪽도 못 찾으면 `match_status='unmatched'` 로 후보만 남기고
  **매수 신호를 만들지 않는다**(웹 화면에서 "언급됐지만 못 찾음"으로 보인다).
* 하루 1회 보장은 이중이다 — 프로세스 내 날짜 캐시 + `trend_scan_run.scan_date` UNIQUE.
  실패(status='error')한 날도 그날의 행이 남으므로 **같은 날 다시 조사하지 않는다**
  (유료 호출 폭주 방지). 다음 날 정상 시도한다.
* 주문 게이트·risk_guard 는 전혀 건드리지 않는다. 이 서비스는 신호까지만 만든다.

수동 재조사 (웹의 "지금 다시 조사" 버튼)
* 웹(`stock_web` 계정)은 `trend_scan_request` 에 `pending` 행을 INSERT 만 한다.
  서버가 `REQUEST_POLL_SEC` 마다 확인해 **원자적으로 claim** 하고 처리한다
  (`TrendRequestWorker`). 자동거래·주문 게이트 상태와 무관하게 동작한다.
* 수동 실행은 `scan_time`·하루 1회 제한을 무시하지만 **관찰 전용**이다 —
  후보는 `trend_scan_candidate` 에 정상 기록되고, 매수 신호는 **아예 만들지 않는다**.
* 결과는 그날의 `trend_scan_run` 행을 덮어쓰고(후보도 통째로 교체), 시도 자체는
  `trend_scan_attempt`(append-only 감사로그)에 영구히 쌓인다.
* **웹 검색이 전부 실패(성공 0건)하면** 2단계 추출이 성공해도 status 를 `partial` 로
  강제한다 — 검색 없이 나온 결과를 'ok' 로 보이게 두지 않는다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from ..algo.base import KIND_ENTRY, Signal, qty_for_amount
from ..llm.client import ClaudeError
from ..llm.trend_prompt import (
    EXTRACT_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
    SCOPE_DOMESTIC,
    SCOPE_DOMESTIC_GLOBAL,
    SCOPE_GLOBAL,
    TREND_OUTPUT_SCHEMA,
    build_extract_prompt,
    build_research_prompt,
    theme_lines,
    validate_trend_output,
)
from ..llm.prompt import OutputSchemaError
from ..util import is_weekday, mask_text, parse_hhmm

log = logging.getLogger(__name__)

CODE = "claude_trend_scan"

MATCH_THEME = "kiwoom_theme_member"
MATCH_NAME = "name_matched"
MATCH_NONE = "unmatched"

STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_ERROR = "error"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"

# 조사 결과 원문을 DB(research_summary)에 남길 때의 상한
RESEARCH_SUMMARY_MAX = 8000

# 웹 검색을 시도했는데 성공한 검색이 0건일 때 남기는 사유(상태를 partial 로 강제)
NO_SEARCH_REASON = "웹 검색이 전부 실패(성공 0건) - 검색 없이 나온 조사라 신뢰도 낮음"

# ---------------------------------------------------------------------- #
# 수동 재조사 요청 큐 (웹의 "지금 다시 조사" 버튼 → trend_scan_request)
# ---------------------------------------------------------------------- #
# 엔진 하트비트(10초)와 비슷한 짧은 주기로 확인한다. 자동거래·주문 게이트 상태와
# **무관하게** 엔진이 돌고 있으면 항상 동작한다.
REQUEST_POLL_SEC = 20
# `processing` 인 채 이만큼 지난 요청은 서버가 중간에 죽은 것으로 보고 error 로 정리한다
STALE_PROCESSING_MIN = 10
STALE_REASON = "서버 재시작으로 중단"

# 알고리즘 인스턴스는 평가 주기마다 새로 만들어지므로 '오늘 실행함' 표시와 대기 신호는
# 모듈 수준에 둔다.
_STATE_LOCK = threading.Lock()
_last_scan_date: _dt.date | None = None
_pending: tuple[_dt.date, list[Signal]] | None = None
_emitted_date: _dt.date | None = None


def reset_state() -> None:
    """'오늘 이미 조사함' 표시와 대기 신호를 지운다(테스트/재설정용)."""
    global _last_scan_date, _pending, _emitted_date
    with _STATE_LOCK:
        _last_scan_date = None
        _pending = None
        _emitted_date = None


def last_scan_date() -> _dt.date | None:
    with _STATE_LOCK:
        return _last_scan_date


def pending_count(day: _dt.date | None = None) -> int:
    with _STATE_LOCK:
        if _pending is None or (day is not None and _pending[0] != day):
            return 0
        return len(_pending[1])


def _mark_scanned(day: _dt.date) -> None:
    global _last_scan_date
    with _STATE_LOCK:
        _last_scan_date = day


def _set_pending(day: _dt.date, signals: list[Signal]) -> None:
    global _pending
    with _STATE_LOCK:
        _pending = (day, list(signals))


def _clear_pending() -> None:
    """대기 중인 매수 신호를 버린다.

    수동 재조사로 그날 결과가 통째로 바뀌면, **이전 조사에서 만든 대기 신호**는
    더 이상 그날의 공식 결과가 아니다. 지워 두면 `_emit_if_tradable` 이 DB 의
    최신 후보(`_restore_pending`)를 쓴다. 지우는 쪽이 항상 더 안전하다(주문이 줄기만 한다).
    """
    global _pending
    with _STATE_LOCK:
        _pending = None


def _take_pending(day: _dt.date) -> list[Signal] | None:
    """오늘 몫 대기 신호를 꺼낸다(한 번만). 오늘 것이 없으면 None."""
    global _pending
    with _STATE_LOCK:
        if _pending is None or _pending[0] != day:
            return None
        signals = _pending[1]
        _pending = None
        return signals


def _mark_emitted(day: _dt.date) -> None:
    global _emitted_date
    with _STATE_LOCK:
        _emitted_date = day


def _already_emitted(day: _dt.date) -> bool:
    with _STATE_LOCK:
        return _emitted_date == day


def trade_window(ctx) -> tuple[_dt.time | None, _dt.time | None]:
    """risk_guard 의 매매 시간(읽기 전용). 알 수 없으면 (None, None)."""
    try:
        rows = ctx.algorithm_rows() or []
    except Exception:  # noqa: BLE001
        return None, None
    for a in rows:
        if a.get("code") == "risk_guard":
            prm = a.get("params") or {}
            return parse_hhmm(prm.get("trade_start_time")), parse_hhmm(prm.get("trade_end_time"))
    return None, None


def in_trade_window(ctx) -> bool:
    """risk_guard 가 매수를 허용하는 시간대인지(신호 투입 시점을 맞추기 위한 참고)."""
    start, end = trade_window(ctx)
    now = ctx.now.time()
    if start is not None and now < start:
        return False
    if end is not None and now > end:
        return False
    return True


def norm_name(text) -> str:
    """테마명/종목명 비교용 정규화 — **공백 제거만** 한다(유사 매칭 금지)."""
    return "".join(str(text or "").split())


def _flu_rt(member: dict) -> float:
    """등락률 정렬 키. 값이 없으면 맨 뒤로."""
    try:
        return float(member.get("flu_rt"))
    except (TypeError, ValueError):
        return -9999.0


class TrendScanParams:
    """알고리즘 파라미터 묶음(ParamSet → 값)."""

    def __init__(self, params):
        scope = params.str("region_scope", SCOPE_DOMESTIC_GLOBAL) or SCOPE_DOMESTIC_GLOBAL
        self.region_scope = scope if scope in (
            SCOPE_DOMESTIC_GLOBAL, SCOPE_DOMESTIC, SCOPE_GLOBAL) else SCOPE_DOMESTIC_GLOBAL
        self.model = params.str("model", "claude-opus-5") or "claude-opus-5"
        self.effort = params.str("effort", "medium") or "medium"
        self.scan_time = params.time("scan_time")
        self.max_domestic_themes = max(1, min(20, params.int("max_domestic_themes", 8)))
        self.max_per_theme = max(1, min(10, params.int("max_candidates_per_theme", 3)))
        self.max_total = max(1, min(50, params.int("max_total_candidates", 10)))
        self.min_confidence = max(0, min(100, params.int("min_confidence", 60)))
        self.buy_amount = max(0, params.int("buy_amount", 100000))
        self.order_type = params.str("order_type", "3") or "3"
        self.max_web_searches = max(1, min(20, params.int("max_web_searches", 6)))
        self.timeout_sec = float(max(30, min(300, params.int("timeout_sec", 90))))

    @property
    def wants_domestic(self) -> bool:
        return self.region_scope in (SCOPE_DOMESTIC_GLOBAL, SCOPE_DOMESTIC)

    @property
    def wants_global(self) -> bool:
        return self.region_scope in (SCOPE_DOMESTIC_GLOBAL, SCOPE_GLOBAL)


class TrendScanResult:
    """스캔 1회 결과(점검 명령·로그용)."""

    def __init__(self, observe_only: bool = False):
        self.run_id: int | None = None
        self.status: str = STATUS_OK
        self.themes: list[dict] = []
        self.candidates: list[dict] = []       # DB 에 기록한 후보 행(+ id)
        self.signals: list[Signal] = []
        self.dropped: int = 0                  # 스키마 위반으로 버린 후보 수
        self.web_search_count: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.latency_ms: int = 0
        self.error_msg: str = ""
        self.research_text: str = ""
        self.theme_summary: str = ""
        # 관찰 전용(수동 재조사): 후보만 기록하고 **매수 신호를 아예 만들지 않는다**
        self.observe_only: bool = bool(observe_only)
        self.search_ok_count: int = 0
        self.search_fail_count: int = 0
        self.started_at: _dt.datetime | None = None
        self.region_scope: str = ""
        self.model: str = ""

    @property
    def matched_count(self) -> int:
        return sum(1 for c in self.candidates if c["match_status"] != MATCH_NONE)

    @property
    def unmatched_count(self) -> int:
        return sum(1 for c in self.candidates if c["match_status"] == MATCH_NONE)


class TrendScanService:
    """하루 1회 트렌드 스캔. 주문은 만들지 않고 **신호까지만** 만든다."""

    def __init__(self, db, market=None, client=None):
        self.db = db
        self.market = market
        self.client = client        # None 이면 claude_advisor 와 같은 공용 클라이언트 사용

    # ------------------------------------------------------------------ #
    def due_reason(self, ctx, p: TrendScanParams) -> str:
        """지금 실행하면 안 되는 이유(빈 문자열이면 실행 가능)."""
        today = ctx.now.date()
        if not is_weekday(today):
            return "주말(토·일) - 실행 생략"
        if last_scan_date() == today:
            return "오늘 이미 조사 완료"
        if p.scan_time is not None and ctx.now.time() < p.scan_time:
            return f"조사 시각({p.scan_time:%H:%M}) 이전"
        if self.market is None:
            return "시세 서비스 없음"
        return ""

    def run_if_due(self, ctx, p: TrendScanParams) -> list[Signal]:
        """조사(하루 1회)와 신호 투입을 함께 처리한다.

        조사는 `scan_time`(기본 08:30, 장 시작 전) 에 하지만, 그때 만든 신호를 바로
        파이프라인에 넣으면 risk_guard 의 '장 시간이 아님 / 매매시간 외'에 막혀 버린다.
        그래서 신호는 **매매가 가능해지는 시점까지 대기**시켰다가 그날 한 번 투입한다.
        """
        today = ctx.now.date()
        self._scan_if_due(ctx, p, today)
        return self._emit_if_tradable(ctx, p, today)

    # ------------------------------------------------------------------ #
    def _scan_if_due(self, ctx, p: TrendScanParams, today: _dt.date) -> None:
        """하루 1회 조건을 확인하고 조사를 실행한다. 결과 신호는 대기열에 넣는다."""
        reason = self.due_reason(ctx, p)
        if reason:
            log.debug("%s 생략: %s", CODE, reason)
            return
        try:
            existing = self.db.trend_scan_run_on(today)
        except Exception:  # noqa: BLE001 - 조회 실패 시 이번 사이클은 생략(다음에 재시도)
            log.warning("%s: 오늘자 스캔 기록 조회 실패 - 이번 주기 생략", CODE, exc_info=True)
            return
        if existing:
            log.info("%s: 오늘(%s) 스캔 기록이 이미 있습니다(status=%s) - 재조사하지 않습니다",
                     CODE, today, existing.get("status"))
            _mark_scanned(today)
            return
        try:
            run_id = self.db.start_trend_scan_run(today, p.region_scope, p.model)
        except Exception as exc:  # noqa: BLE001 - UNIQUE 충돌 = 다른 인스턴스가 이미 시작
            log.warning("%s: 오늘자 스캔 행 생성 실패(중복 실행 방지): %s", CODE, type(exc).__name__)
            _mark_scanned(today)
            return
        # 행을 만든 시점에 '오늘 몫'을 소진한 것으로 본다(실패해도 같은 날 재시도하지 않는다)
        _mark_scanned(today)
        result = self.scan(ctx, p, run_id)
        _set_pending(today, result.signals)

    # ------------------------------------------------------------------ #
    def _emit_if_tradable(self, ctx, p: TrendScanParams, today: _dt.date) -> list[Signal]:
        """대기 중인 매수 신호를 **그날 한 번** 파이프라인에 넘긴다.

        같은 신호를 매 주기 다시 내보내면 중복 주문 위험이 있어 1회만 투입한다.
        프로세스가 조사 뒤 재기동되면 대기열이 비므로, 그때는 오늘 기록해 둔
        `trend_scan_candidate` 에서 아직 신호가 붙지 않은 확정 종목을 되살린다.
        """
        if _already_emitted(today) or last_scan_date() != today:
            return []
        if not ctx.market_open or not in_trade_window(ctx):
            return []
        signals = _take_pending(today)
        if signals is None:
            signals = self._restore_pending(ctx, p, today)
        _mark_emitted(today)
        if signals:
            log.info("%s: 대기 중이던 매수 후보 %d건을 파이프라인에 투입합니다",
                     CODE, len(signals))
        return signals

    def _restore_pending(self, ctx, p: TrendScanParams, today: _dt.date) -> list[Signal]:
        """재기동 대비: 오늘 기록된 후보 중 아직 신호가 붙지 않은 확정 종목을 신호로 되살린다."""
        try:
            run_row = self.db.trend_scan_run_on(today)
            rows = self.db.trend_scan_candidates(int(run_row["id"])) if run_row else []
        except Exception:  # noqa: BLE001 - 복구 실패는 '후보 없음'으로 본다
            log.warning("%s: 후보 복구 실패", CODE, exc_info=True)
            return []
        out: list[Signal] = []
        seen: set[str] = set()
        for row in rows:
            code = row.get("stk_cd")
            if not code or code in seen or row.get("signal_id"):
                continue
            if row.get("match_status") == MATCH_NONE or code in ctx.holdings:
                continue
            if ctx.untradable_reason(code, row.get("stk_nm")):
                continue
            cand = {"region": row.get("region") or "domestic", "theme": row.get("theme") or "-",
                    "rationale": row.get("rationale") or "",
                    "confidence": int(row.get("confidence") or 0)}
            pick = {"stk_cd": code, "stk_nm": row.get("stk_nm"),
                    "cur_prc": self._price(ctx, code, {}),
                    "match_status": row.get("match_status") or MATCH_NAME}
            signal = self._make_signal(p, cand, pick)
            if signal is None:
                continue
            if row.get("id"):
                signal.meta["trend_candidate_id"] = row["id"]
            seen.add(code)
            out.append(signal)
        if out:
            log.info("%s: 재기동 후 오늘 후보 %d건을 DB 에서 되살렸습니다", CODE, len(out))
        return out

    def run_forced(self, ctx, p: TrendScanParams) -> TrendScanResult:
        """점검 명령(`--trend-scan-check`)용: 스케줄·중복방지를 무시하고 1회 실행한다.

        감사 추적을 위해 **DB 에는 정상적으로 기록**한다(오늘 행이 있으면 그 행을 갱신).
        """
        today = ctx.now.date()
        run_id = None
        try:
            existing = self.db.trend_scan_run_on(today)
            run_id = int(existing["id"]) if existing else self.db.start_trend_scan_run(
                today, p.region_scope, p.model)
        except Exception:  # noqa: BLE001 - 기록 실패가 점검 자체를 막지 않게
            log.warning("%s: 스캔 행 준비 실패 - 기록 없이 진행", CODE, exc_info=True)
        result = self.scan(ctx, p, run_id)
        _mark_scanned(today)
        return result

    def run_manual(self, ctx, p: TrendScanParams, requested_by: str = "") -> TrendScanResult:
        """웹의 "지금 다시 조사" 요청용: **관찰 전용**으로 1회 강제 실행한다.

        * `scan_time` 시각 제한과 '오늘 이미 조사함' 제한을 **둘 다 무시**한다
          (그게 수동 재조사의 목적이다).
        * `--trend-scan-check` 와 같은 관찰 전용 경로를 쓴다 — 호출자가 게이트를 닫은
          컨텍스트를 넘기고, 여기서는 **매수 신호를 아예 만들지 않는다**.
        * 중간 기록을 하지 않는다(`run_id=None`). 결과는 호출자가 한 트랜잭션으로
          `trend_scan_run` UPSERT + 후보 교체에 반영한다.
        """
        today = ctx.now.date()
        log.warning("%s: 수동 재조사 실행 (요청자=%s, 관찰 전용 - 주문/신호 없음)",
                    CODE, requested_by or "-")
        result = self.scan(ctx, p, None, observe_only=True)
        # 이전 조사에서 남은 대기 신호는 더 이상 그날의 공식 결과가 아니다
        _clear_pending()
        _mark_scanned(today)
        return result

    # ------------------------------------------------------------------ #
    def scan(self, ctx, p: TrendScanParams, run_id: int | None,
             observe_only: bool = False) -> TrendScanResult:
        """스캔 1회. 예외를 밖으로 던지지 않고 status='error' 로 기록한다.

        `observe_only=True`(수동 재조사) 면 후보 행만 만들고 **매수 신호를 만들지 않는다**
        — 자동거래·주문 게이트 상태와 무관하게 `signal_log`/`orders` 에 아무것도 남지 않는다.
        """
        result = TrendScanResult(observe_only=observe_only)
        result.run_id = run_id
        result.started_at = getattr(ctx, "now", None)
        result.region_scope = p.region_scope
        result.model = p.model
        try:
            self._scan(ctx, p, result)
        except ClaudeError as exc:
            result.status = STATUS_ERROR
            result.error_msg = f"Claude 오류: {exc}"
            log.error("%s: %s", CODE, result.error_msg)
        except Exception as exc:  # noqa: BLE001 - 스캔 실패가 엔진을 죽이지 않게
            result.status = STATUS_ERROR
            result.error_msg = f"{type(exc).__name__}: {mask_text(str(exc))[:150]}"
            log.exception("%s 실패", CODE)
        if result.status == STATUS_ERROR:
            result.signals = []
        self._finish(run_id, result, ctx.now.date())
        self._log_event(ctx, result)
        return result

    # ------------------------------------------------------------------ #
    def _scan(self, ctx, p: TrendScanParams, result: TrendScanResult) -> None:
        # ---- ① 국내 테마(ka90001, 읽기 전용) --------------------------- #
        themes: list[dict] = []
        if p.wants_domestic:
            themes = list(self.market.themes() or [])
        result.themes = themes
        lines = theme_lines(themes, p.max_domestic_themes)
        result.theme_summary = "\n".join(lines)

        # ---- ② 1단계: 웹 검색 조사 ------------------------------------ #
        client = self._client(ctx)
        prompt = build_research_prompt(now=ctx.now, region_scope=p.region_scope,
                                       theme_lines_text=lines, max_themes=p.max_domestic_themes)
        research = client.research(prompt, model=p.model, system=RESEARCH_SYSTEM_PROMPT,
                                   effort=p.effort, timeout_sec=p.timeout_sec,
                                   max_web_searches=p.max_web_searches)
        result.research_text = research.text[:RESEARCH_SUMMARY_MAX]
        result.web_search_count = research.web_search_count
        result.search_ok_count = research.search_ok_count
        result.search_fail_count = research.search_fail_count
        result.input_tokens += research.input_tokens
        result.output_tokens += research.output_tokens
        result.latency_ms += research.latency_ms

        # ---- ③ 2단계: 구조화 추출 + 로컬 재검증 ------------------------ #
        extract = client.extract(
            build_extract_prompt(research_text=research.text,
                                 theme_names=[t["thema_nm"] for t in themes[:p.max_domestic_themes]]),
            model=p.model, system=EXTRACT_SYSTEM_PROMPT, schema=TREND_OUTPUT_SCHEMA,
            timeout_sec=p.timeout_sec)
        result.input_tokens += extract.input_tokens
        result.output_tokens += extract.output_tokens
        result.latency_ms += extract.latency_ms
        try:
            candidates, dropped = validate_trend_output(extract.data,
                                                        max_candidates=p.max_total * 3)
        except OutputSchemaError as exc:
            raise ClaudeError(f"응답 스키마 위반: {exc}", infra=False) from None
        result.dropped = dropped
        if dropped:
            result.status = STATUS_PARTIAL
        if research.search_errors:
            result.status = STATUS_PARTIAL
        # 검색을 시도했는데 **성공한 검색이 0건**이면 2단계 추출이 스키마상 성공했더라도
        # 이번 조사는 실질적으로 실패한 것이다(모델의 사전 지식만으로 쓴 보고서).
        # 상태로 그 사실을 드러낸다 — 웹 화면이 'ok' 로 보고 신뢰하면 안 된다.
        if research.web_search_all_failed:
            result.status = STATUS_PARTIAL
            detail = ", ".join(research.search_errors[:3])
            result.error_msg = (f"{NO_SEARCH_REASON}"
                                f"{f' ({detail})' if detail else ''}")[:200]
            log.error("%s: %s", CODE, result.error_msg)

        # ---- ④ 종목 확정 + 신호 ---------------------------------------- #
        self._resolve(ctx, p, candidates, themes, result)

    # ------------------------------------------------------------------ #
    def _resolve(self, ctx, p: TrendScanParams, candidates: list[dict],
                 themes: list[dict], result: TrendScanResult) -> None:
        """후보 테마 → 실제 종목. 키움 테마 구성종목 또는 종목마스터 이름 정확일치만 쓴다."""
        by_name = {norm_name(t["thema_nm"]): t for t in themes}
        used: set[str] = set()
        confirmed = 0

        # 확신도가 높은 테마부터 처리한다 — 일 상한(max_total_candidates)을 낮은 확신도 후보가
        # 먼저 채워 버리지 않게 한다(동점이면 모델이 준 순서 유지).
        for cand in sorted(candidates, key=lambda c: c["confidence"], reverse=True):
            if confirmed >= p.max_total:
                log.info("%s: 일 최대 후보 종목수(%d) 도달 - 남은 테마는 건너뜁니다",
                         CODE, p.max_total)
                break
            theme = by_name.get(norm_name(cand.get("kiwoom_theme_name_guess")))
            picks: list[dict] = []
            if theme is not None:
                picks = self._theme_picks(ctx, p, theme, used)
            if not picks:
                picks = self._name_picks(ctx, p, cand, used)
            for pick in picks:
                if pick["match_status"] != MATCH_NONE:
                    if confirmed >= p.max_total:
                        break              # 상한 초과분은 기록도 신호도 만들지 않는다
                    confirmed += 1
                    used.add(pick["stk_cd"])
                self._record(ctx, p, cand, theme, pick, result)

    # ------------------------------------------------------------------ #
    def _theme_picks(self, ctx, p: TrendScanParams, theme: dict,
                     used: set[str]) -> list[dict]:
        """ka90002 구성종목 중 등락률 상위 N (거래불가·보유·중복 제외)."""
        try:
            members = list(self.market.theme_members(theme["thema_grp_cd"]) or [])
        except Exception:  # noqa: BLE001 - 조회 실패는 이름 매칭으로 넘어간다
            log.warning("%s: 테마 구성종목 조회 실패 (%s)", CODE, theme.get("thema_nm"),
                        exc_info=True)
            return []
        members.sort(key=_flu_rt, reverse=True)
        out: list[dict] = []
        for m in members:
            if len(out) >= p.max_per_theme:
                break
            code, name = m.get("stk_cd"), m.get("stk_nm") or ""
            if not code or code in used:
                continue
            if code in ctx.holdings:
                continue                      # 이미 보유 → 물타기 알고리즘 담당
            if ctx.untradable_reason(code, name):
                continue                      # R-06 거래불가 종목 제외
            price = int(m.get("cur_prc") or 0)
            if price <= 0:
                continue
            out.append({"stk_cd": code, "stk_nm": name, "cur_prc": price,
                        "match_status": MATCH_THEME})
        return out

    def _name_picks(self, ctx, p: TrendScanParams, cand: dict,
                    used: set[str]) -> list[dict]:
        """회사명 → `stock_master.stk_nm` **정확 일치**. 못 찾으면 unmatched 행만 만든다."""
        out: list[dict] = []
        for name in cand.get("company_names") or []:
            if len(out) >= p.max_per_theme:
                break
            try:
                rows = self.db.find_stocks_by_name(name) or []
            except Exception:  # noqa: BLE001 - 조회 실패는 '못 찾음'으로 본다(fail-closed)
                log.warning("%s: 종목명 조회 실패 (%s)", CODE, name, exc_info=True)
                rows = []
            row = next((r for r in rows if r.get("stk_cd")), None)
            if row is None:
                out.append({"stk_cd": None, "stk_nm": name, "cur_prc": 0,
                            "match_status": MATCH_NONE})
                continue
            code = str(row["stk_cd"])
            if code in used or code in ctx.holdings:
                continue
            if ctx.untradable_reason(code, row.get("stk_nm")):
                out.append({"stk_cd": None, "stk_nm": name, "cur_prc": 0,
                            "match_status": MATCH_NONE})
                continue
            price = self._price(ctx, code, row)
            out.append({"stk_cd": code, "stk_nm": row.get("stk_nm") or name,
                        "cur_prc": price, "match_status": MATCH_NAME})
        return out

    @staticmethod
    def _price(ctx, stk_cd: str, row: dict) -> int:
        """현재가. 시세 조회가 안 되면 종목마스터의 전일종가로 대체한다."""
        try:
            price = ctx.current_price(stk_cd)
        except Exception:  # noqa: BLE001
            price = None
        if not price:
            try:
                price = int(row.get("last_price") or 0)
            except (TypeError, ValueError):
                price = 0
        return max(0, int(price or 0))

    # ------------------------------------------------------------------ #
    def _record(self, ctx, p: TrendScanParams, cand: dict, theme: dict | None,
                pick: dict, result: TrendScanResult) -> dict | None:
        """후보 1건을 DB 에 남기고, 조건을 만족하면 매수 신호를 만든다."""
        row = {
            "region": cand["region"],
            "theme": cand["theme"],
            "rationale": cand.get("rationale") or "",
            "confidence": cand["confidence"],
            "kiwoom_theme_cd": (theme or {}).get("thema_grp_cd")
            if pick["match_status"] == MATCH_THEME else None,
            "kiwoom_theme_nm": (theme or {}).get("thema_nm")
            if pick["match_status"] == MATCH_THEME else None,
            "stk_cd": pick["stk_cd"],
            "stk_nm": pick["stk_nm"],
            "match_status": pick["match_status"],
        }
        # 관찰 전용 경로는 **신호를 만들지 않는다**(게이트·자동거래 상태와 무관하게 차단).
        signal = None if result.observe_only else self._make_signal(p, cand, pick)
        candidate_id = None
        try:
            if result.run_id:
                candidate_id = self.db.insert_trend_candidate(result.run_id, **row)
        except Exception:  # noqa: BLE001 - 기록 실패가 신호 생성을 막지 않게
            log.warning("%s: 후보 기록 실패", CODE, exc_info=True)
        row["id"] = candidate_id
        result.candidates.append(row)
        if signal is not None:
            if candidate_id:
                signal.meta["trend_candidate_id"] = candidate_id
            result.signals.append(signal)
        return row

    def _make_signal(self, p: TrendScanParams, cand: dict, pick: dict) -> Signal | None:
        """매수 신호 조건: 확정 종목 + 확신도 충족 + 수량 ≥ 1."""
        if pick["match_status"] == MATCH_NONE or not pick["stk_cd"]:
            return None
        if cand["confidence"] < p.min_confidence:
            log.info("%s: 확신도 미달로 신호 없음 (%s %s, %d < %d)", CODE, pick["stk_cd"],
                     pick["stk_nm"], cand["confidence"], p.min_confidence)
            return None
        price = int(pick.get("cur_prc") or 0)
        qty = qty_for_amount(p.buy_amount, price)
        if qty <= 0:
            log.info("%s: 1주 값(%s원)이 매수금액(%s원)보다 커서 신호 없음 (%s)",
                     CODE, f"{price:,}", f"{p.buy_amount:,}", pick["stk_cd"])
            return None
        reason = (f"산업 트렌드 [{cand['theme']}] {cand['rationale']}"
                  f" (확신도 {cand['confidence']}, 매칭 {pick['match_status']})")
        return Signal(
            algo_code=CODE,
            stk_cd=pick["stk_cd"],
            stk_nm=pick["stk_nm"],
            side="BUY",
            qty=qty,
            price=None if p.order_type == "3" else price,
            trde_tp=p.order_type,
            amount=qty * price,
            kind=KIND_ENTRY,
            score=float(cand["confidence"]),
            reason=reason[:500],
            meta={"cur_prc": price, "theme": cand["theme"],
                  "match_status": pick["match_status"]},
        )

    # ------------------------------------------------------------------ #
    def _client(self, ctx):
        if self.client is not None:
            return self.client
        from ..algo.claude_advisor import get_client

        cfg = getattr(ctx, "anthropic_cfg", None)
        if cfg is None:
            raise ClaudeError("Anthropic 설정이 없습니다([anthropic] apikey_file)", infra=False)
        return get_client(cfg)

    def _finish(self, run_id: int | None, result: TrendScanResult,
                day: _dt.date | None = None) -> None:
        if not run_id:
            return
        # 시도 감사로그(append-only)는 결과 행과 별개로 **모든 시도**를 남긴다.
        # (수동 재조사는 TrendRequestWorker 가 직접 남긴다)
        if day is not None and not result.observe_only:
            try:
                self.db.insert_trend_scan_attempt(
                    day, trigger_type=TRIGGER_SCHEDULED, status=result.status,
                    region_scope=result.region_scope or None, model=result.model or None,
                    candidate_count=len(result.candidates),
                    web_search_count=result.web_search_count,
                    input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                    latency_ms=result.latency_ms, error_msg=result.error_msg or None,
                    research_summary=result.research_text or None,
                    started_at=result.started_at)
            except Exception:  # noqa: BLE001 - 감사로그 실패가 결과 기록을 막지 않게
                log.warning("%s: 시도 감사로그 기록 실패", CODE, exc_info=True)
        try:
            self.db.finish_trend_scan_run(
                run_id, status=result.status, candidate_count=len(result.candidates),
                signal_count=len(result.signals),
                domestic_theme_summary=result.theme_summary or None,
                research_summary=result.research_text or None,
                web_search_count=result.web_search_count,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                latency_ms=result.latency_ms, error_msg=result.error_msg or None)
        except Exception:  # noqa: BLE001
            log.warning("%s: 스캔 결과 기록 실패", CODE, exc_info=True)

    @staticmethod
    def _log_event(ctx, result: TrendScanResult) -> None:
        level = "ERROR" if result.status == STATUS_ERROR else "INFO"
        msg = (f"산업 트렌드 스캔 {result.status}: 테마 {len(result.themes)}개 참고, "
               f"후보 {len(result.candidates)}건(확정 {result.matched_count} / "
               f"미매칭 {result.unmatched_count}), 신호 {len(result.signals)}건, "
               f"웹검색 {result.web_search_count}회, 토큰 입력 {result.input_tokens:,}/"
               f"출력 {result.output_tokens:,}")
        if result.error_msg:
            msg += f" | {result.error_msg}"
        log.info("%s", msg) if level == "INFO" else log.error("%s", msg)
        try:
            ctx.db.log_event(level, "algo", msg)
        except Exception:  # noqa: BLE001
            log.debug("트렌드 스캔 이벤트 기록 실패", exc_info=True)


class TrendRequestWorker:
    """웹의 "지금 다시 조사" 요청(`trend_scan_request`) 처리기.

    엔진 루프가 `REQUEST_POLL_SEC`(기본 20초)마다 `poll_once()` 를 부른다.
    **자동거래 ON/OFF·주문 게이트와 무관하게** 엔진이 돌고 있으면 항상 동작한다
    (웹은 Anthropic/키움 자격증명이 없어 직접 실행할 수 없다).

    안전 원칙
    * 동시에 **1건만** 처리한다 — 이미 `processing` 인 요청이 있으면 새 pending 을 집지
      않는다(유료 호출 낭비 방지).
    * claim 은 `UPDATE ... WHERE id=%s AND status='pending'` 의 영향 행 수로 판정한다
      (여러 프로세스/스레드가 경쟁해도 한 곳만 이긴다).
    * 실행은 **관찰 전용**이다 — 후보는 `trend_scan_candidate` 에 정상 기록되지만
      매수 신호를 만들지 않으므로 `signal_log`/`orders` 에는 아무것도 남지 않는다.
    * 어떤 예외도 밖으로 던지지 않는다(엔진 루프가 죽지 않게).
    """

    def __init__(self, db, ctx_factory, market=None, client=None):
        self.db = db
        self.ctx_factory = ctx_factory     # () -> EngineContext (관찰 전용 - 게이트 닫힘)
        self.market = market
        self.client = client
        self.processed = 0

    # ------------------------------------------------------------------ #
    def cleanup_stale(self, minutes: int = STALE_PROCESSING_MIN) -> int:
        """`processing` 인 채 멈춘 요청을 정리한다(서버가 처리 중 죽은 경우)."""
        n = self.db.expire_stale_trend_requests(minutes, STALE_REASON)
        if n:
            log.warning("%s: 멈춰 있던 수동 재조사 요청 %d건을 오류로 정리했습니다(%d분 경과)",
                        CODE, n, minutes)
            try:
                self.db.log_event("WARN", "algo",
                                  f"수동 재조사 요청 {n}건 정리 ({STALE_REASON})")
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
            if self.db.count_processing_trend_requests() > 0:
                log.debug("%s: 이미 처리 중인 재조사 요청이 있어 건너뜁니다", CODE)
                return None
            req = self.db.claim_trend_scan_request()
        except Exception:  # noqa: BLE001 - 조회 실패는 다음 주기에 재시도
            log.warning("%s: 수동 재조사 요청 조회 실패", CODE, exc_info=True)
            return None
        if req is None:
            return None
        return self._process(req)

    # ------------------------------------------------------------------ #
    def _process(self, req: dict) -> dict:
        req_id = int(req["id"])
        who = str(req.get("requested_by") or "")[:50]
        out = {"request_id": req_id, "requested_by": who, "status": STATUS_ERROR,
               "run_id": None, "candidates": 0, "signals": 0, "error": ""}
        log.warning("%s: 수동 재조사 요청 처리 시작 (id=%d, 요청자=%s)", CODE, req_id, who or "-")
        try:
            ctx = self.ctx_factory()
            p = self.load_params()
            svc = TrendScanService(self.db, market=self.market, client=self.client)
            result = svc.run_manual(ctx, p, who)
            run_id = self._persist(ctx.now.date(), p, result, who)
            out.update(status=result.status, run_id=run_id,
                       candidates=len(result.candidates), signals=len(result.signals),
                       error=result.error_msg)
            self._finish(req_id, "error" if result.status == STATUS_ERROR else "done",
                         run_id=run_id, error_msg=result.error_msg or None)
            self.processed += 1
            log.warning("%s: 수동 재조사 완료 (id=%d, status=%s, 후보 %d건, run_id=%s)",
                        CODE, req_id, result.status, len(result.candidates), run_id)
            try:
                self.db.log_event(
                    "ERROR" if result.status == STATUS_ERROR else "INFO", "algo",
                    f"수동 재조사({who or '-'}) {result.status}: 후보 "
                    f"{len(result.candidates)}건 (관찰 전용 - 주문/신호 없음)"
                    + (f" | {result.error_msg}" if result.error_msg else ""))
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001 - 요청 1건의 실패가 루프를 죽이지 않게
            msg = f"{type(exc).__name__}: {mask_text(str(exc))[:150]}"
            out["error"] = msg
            log.exception("%s: 수동 재조사 요청 처리 실패 (id=%d)", CODE, req_id)
            # `_persist`(=attempt 감사로그 append)까지 못 간 실패다(ctx_factory/load_params
            # 등에서 죽은 경우). 웹은 trend_scan_request.status 를 직접 조회하지 않고
            # trend_scan_attempt 에 새 행이 생기는 것으로 "처리 끝남"을 판단하므로,
            # 여기서도 한 줄 남기지 않으면 웹 화면이 영원히 "처리 중"으로 보인다.
            try:
                self.db.insert_trend_scan_attempt(
                    _dt.date.today(), trigger_type=TRIGGER_MANUAL, requested_by=who or None,
                    status=STATUS_ERROR, error_msg=msg, started_at=None)
            except Exception:  # noqa: BLE001 - 이중 방어. 이것마저 실패해도 요청 상태는 갱신한다
                log.warning("%s: 조기 실패 감사로그 기록도 실패", CODE, exc_info=True)
            self._finish(req_id, "error", error_msg=msg)
        return out

    def _finish(self, req_id: int, status: str, run_id: int | None = None,
                error_msg: str | None = None) -> None:
        try:
            self.db.finish_trend_scan_request(req_id, status=status, run_id=run_id,
                                              error_msg=error_msg)
        except Exception:  # noqa: BLE001 - 상태 갱신 실패는 stale 정리가 나중에 풀어 준다
            log.warning("%s: 재조사 요청 상태 갱신 실패 (id=%d)", CODE, req_id, exc_info=True)

    # ------------------------------------------------------------------ #
    def load_params(self) -> TrendScanParams:
        """DB 의 알고리즘 파라미터를 그대로 쓴다(선택 여부와 무관 - 수동 실행이므로)."""
        from ..algo.params import ParamSet

        meta = next((a for a in (self.db.load_algorithms() or [])
                     if a.get("code") == CODE), None)
        if meta is None:
            raise RuntimeError(f"DB 에 {CODE} 알고리즘이 없습니다(db/seed.sql 적용 필요)")
        params = ParamSet(meta.get("param_defs") or [], meta.get("params") or {})
        if params.invalid:
            raise RuntimeError(f"{CODE} 파라미터 오류: {'; '.join(params.invalid[:3])}")
        return TrendScanParams(params)

    def _persist(self, day: _dt.date, p: TrendScanParams, result: TrendScanResult,
                 who: str) -> int | None:
        """(a) 시도 감사로그 append → (b) 오늘 run UPSERT + (c) 후보 교체(한 트랜잭션)."""
        try:
            self.db.insert_trend_scan_attempt(
                day, trigger_type=TRIGGER_MANUAL, requested_by=who or None,
                status=result.status, region_scope=p.region_scope, model=p.model,
                candidate_count=len(result.candidates),
                web_search_count=result.web_search_count,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                latency_ms=result.latency_ms, error_msg=result.error_msg or None,
                research_summary=result.research_text or None,
                started_at=result.started_at)
        except Exception:  # noqa: BLE001 - 감사로그 실패가 결과 반영을 막지 않게
            log.warning("%s: 시도 감사로그 기록 실패", CODE, exc_info=True)
        run_id = self.db.save_manual_trend_scan(
            day, requested_by=who or None, status=result.status,
            region_scope=p.region_scope, model=p.model, candidates=result.candidates,
            domestic_theme_summary=result.theme_summary or None,
            research_summary=result.research_text or None,
            web_search_count=result.web_search_count, input_tokens=result.input_tokens,
            output_tokens=result.output_tokens, latency_ms=result.latency_ms,
            error_msg=result.error_msg or None, started_at=result.started_at)
        result.run_id = run_id
        return run_id


def link_signal(db, candidate_id, signal_id) -> None:
    """Executor 가 남긴 `signal_log.id` 를 후보 행에 연결한다(웹 조회용)."""
    if not candidate_id or not signal_id:
        return
    try:
        db.set_trend_candidate_signal(int(candidate_id), int(signal_id))
    except Exception:  # noqa: BLE001 - 기록용이라 실패해도 주문 흐름에 영향 없음
        log.debug("트렌드 후보-신호 연결 실패", exc_info=True)
