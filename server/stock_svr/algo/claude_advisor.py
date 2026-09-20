"""claude_advisor — Claude 거부권(veto) 필터.

파이프라인 위치 (runner):
    진입 알고리즘 → 필터 → risk_guard → **★ Claude 검토 ★** → Executor(주문 게이트)

즉 앞 단계를 모두 통과해 **곧 주문될 매수(BUY) 신호에만** 호출한다(비용 절감).
* **매도(SELL)·손절·청산 신호는 절대 검토하지 않는다** — 지연·차단 모두 없다.
* 자동거래가 중지 상태이면 runner 가 평가 자체를 하지 않으므로 호출 0회.
* 이 알고리즘이 비활성이면 runner 가 인스턴스를 만들지 않으므로 호출 0회.
* 주문 게이트 진리표(`OrderGateState.can_send_order`)와 Executor 게이트 로직은 건드리지 않는다.
  검토가 pass 일 때만 Executor 로 진행한다.

파라미터(seed.sql): model, effort, min_confidence, fail_mode, cache_minutes,
max_calls_per_day, timeout_sec, review_averaging_down
"""
from __future__ import annotations

import json
import logging
import threading
import time

from ..llm.client import EFFORTS, MODELS, ClaudeClient, ClaudeError, ClaudeReview
from ..llm.prompt import MAX_BARS, build_payload, collect_context
from .base import KIND_AVG_DOWN, KIND_ENTRY, Algorithm, Signal
from .registry import register

log = logging.getLogger(__name__)

CODE = "claude_advisor"

# 검토 대상 신호 종류 (매수 신호만. 손절/청산/익절은 모두 SELL 이라 애초에 제외된다)
REVIEWABLE_KINDS = (KIND_ENTRY, KIND_AVG_DOWN)

FAIL_BLOCK = "block"
FAIL_ALLOW = "allow"

# 입력 요약(JSON) DB 저장 상한
INPUT_SUMMARY_MAX = 20000

# 알고리즘 인스턴스는 평가 주기마다 새로 만들어지므로 캐시/클라이언트는 모듈 수준에 둔다.
_CACHE: dict[tuple[str, str, str], tuple[float, bool, str]] = {}
_CACHE_LOCK = threading.Lock()
_CLIENT_LOCK = threading.Lock()
_CLIENT: tuple[str, int, ClaudeClient] | None = None


def reset_state() -> None:
    """캐시와 클라이언트를 비운다(테스트/설정 변경용)."""
    global _CLIENT
    with _CACHE_LOCK:
        _CACHE.clear()
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT[2].close()
            except Exception:  # noqa: BLE001
                pass
        _CLIENT = None


def get_client(cfg, factory=None) -> ClaudeClient:
    """설정(AnthropicConfig)에 맞는 클라이언트를 재사용한다. 키 값은 노출하지 않는다."""
    global _CLIENT
    path = str(getattr(cfg, "apikey_file", "") or "")
    retries = int(getattr(cfg, "max_retries", 1) or 0)
    with _CLIENT_LOCK:
        if _CLIENT is not None and _CLIENT[0] == path and _CLIENT[1] == retries:
            return _CLIENT[2]
        if _CLIENT is not None:
            try:
                _CLIENT[2].close()
            except Exception:  # noqa: BLE001
                pass
        client = ClaudeClient(cfg.read_key, max_retries=retries, client_factory=factory)
        _CLIENT = (path, retries, client)
        return client


@register
class ClaudeAdvisor(Algorithm):
    code = CODE
    role = "filter"
    name = "Claude 거부권 필터"

    # -- 파라미터 ------------------------------------------------------- #
    @property
    def model(self) -> str:
        value = self.params.str("model", MODELS[0]) or MODELS[0]
        return value if value in MODELS else MODELS[0]

    @property
    def effort(self) -> str:
        value = self.params.str("effort", "low") or "low"
        return value if value in EFFORTS else "low"

    @property
    def min_confidence(self) -> int:
        return max(0, min(100, self.params.int("min_confidence", 70)))

    @property
    def fail_mode(self) -> str:
        value = self.params.str("fail_mode", FAIL_BLOCK) or FAIL_BLOCK
        return FAIL_ALLOW if value == FAIL_ALLOW else FAIL_BLOCK

    @property
    def cache_sec(self) -> int:
        return max(0, self.params.int("cache_minutes", 30)) * 60

    @property
    def max_calls_per_day(self) -> int:
        return max(0, self.params.int("max_calls_per_day", 50))

    @property
    def timeout_sec(self) -> float:
        return float(max(5, min(120, self.params.int("timeout_sec", 30))))

    @property
    def review_averaging_down(self) -> bool:
        return self.params.bool("review_averaging_down", True)

    # ------------------------------------------------------------------ #
    def filter_signals(self, ctx, signals: list[Signal]) -> list[Signal]:
        """일반 필터 단계에서는 아무것도 하지 않는다.

        Claude 검토는 risk_guard **뒤**에서 `review()` 로만 수행된다(runner 참조).
        """
        return signals

    # ------------------------------------------------------------------ #
    @staticmethod
    def needs_review(signal: Signal) -> bool:
        """검토 대상인지. 매도/손절/청산은 **어떤 경우에도** False."""
        return signal.side == "BUY" and signal.kind in REVIEWABLE_KINDS

    # ------------------------------------------------------------------ #
    def review(self, ctx, signal: Signal, client=None) -> tuple[bool, str]:
        """검토 1건. `(통과여부, 사유)`. 예외를 밖으로 던지지 않는다."""
        try:
            return self._review(ctx, signal, client)
        except Exception as exc:  # noqa: BLE001 - 검토 자체가 실패해도 사이클은 계속
            # 내부 오류는 인프라 오류가 아니므로 fail_mode 와 무관하게 차단한다(fail-closed)
            log.exception("claude_advisor 내부 오류")
            return False, f"Claude 차단: 검토 내부 오류({type(exc).__name__})"

    # ------------------------------------------------------------------ #
    def _review(self, ctx, signal: Signal, client) -> tuple[bool, str]:
        if not self.needs_review(signal):
            return True, ""
        if signal.kind == KIND_AVG_DOWN and not self.review_averaging_down:
            return True, ""

        key = (signal.stk_cd, signal.side, signal.algo_code)
        cached = self._cache_get(key)
        if cached is not None:
            ok, detail = cached
            self._log_decision(ctx, signal, decision="allow" if ok else "block",
                               final="pass" if ok else "block", confidence=None,
                               reasons=detail, risk_flags="", payload=None, from_cache=True)
            log.info("claude_advisor 캐시 적중: %s %s", signal.stk_cd, "통과" if ok else "차단")
            return ok, detail

        cap = self.max_calls_per_day
        used = self._calls_today(ctx)
        if cap <= 0 or (used is not None and used >= cap):
            return self._apply_fail_mode(ctx, signal, "일 호출 상한", f"{used}/{cap}회", None)

        payload = self._build_payload(ctx, signal)
        try:
            cli = client if client is not None else get_client(self._anthropic_cfg(ctx))
            result = cli.review(payload, model=self.model, effort=self.effort,
                                timeout_sec=self.timeout_sec)
        except ClaudeError as exc:
            reason = "인프라 오류" if exc.infra else "응답 오류"
            return self._apply_fail_mode(ctx, signal, reason, str(exc), payload,
                                         infra=exc.infra)

        return self._decide(ctx, signal, result, payload)

    # ------------------------------------------------------------------ #
    def _decide(self, ctx, signal: Signal, result: ClaudeReview, payload: dict) -> tuple[bool, str]:
        reasons = result.reasons_text
        flags = ", ".join(result.risk_flags)
        if result.decision == "block":
            detail = f"Claude 차단: {reasons} (확신도 {result.confidence})"
            ok = False
        elif result.confidence < self.min_confidence:
            detail = (f"Claude 차단: 확신도 부족 {result.confidence} < {self.min_confidence} "
                      f"({reasons})")
            ok = False
        else:
            detail = f"Claude 통과: {reasons} (확신도 {result.confidence})"
            ok = True

        self._log_decision(ctx, signal, decision=result.decision,
                           final="pass" if ok else "block", confidence=result.confidence,
                           reasons=reasons, risk_flags=flags, payload=payload,
                           model=result.model, latency_ms=result.latency_ms,
                           input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        self._log_usage(ctx)
        self._cache_put((signal.stk_cd, signal.side, signal.algo_code), ok, detail)
        log.info("claude_advisor %s: %s", "통과" if ok else "차단", detail)
        return ok, detail

    def _apply_fail_mode(self, ctx, signal: Signal, kind: str, detail_text: str,
                         payload: dict | None, infra: bool = True) -> tuple[bool, str]:
        """오류/상한 시 처리. `fail_mode=allow` 는 **인프라 오류에만** 적용한다."""
        allow = (self.fail_mode == FAIL_ALLOW) and infra
        detail = (f"Claude {'통과(오류 허용)' if allow else '차단'}: {kind} - {detail_text}")
        self._log_decision(ctx, signal, decision="error",
                           final="pass" if allow else "block", confidence=None,
                           reasons=kind, risk_flags="", payload=payload,
                           error_msg=f"{kind}: {detail_text}")
        if allow:
            log.warning("claude_advisor 오류지만 fail_mode=allow 로 통과: %s | %s", kind, detail_text)
        else:
            log.warning("claude_advisor 차단(%s): %s", kind, detail_text)
        # 오류 결과는 캐시하지 않는다(다음 사이클에 다시 시도)
        return allow, detail

    # ------------------------------------------------------------------ #
    def _build_payload(self, ctx, signal: Signal) -> dict:
        """입력 JSON 구성. 계좌번호·잔고·예수금·보유수량·키/토큰은 넣지 않는다."""
        bars = self._bars(ctx, signal.stk_cd)
        quote = self._quote(ctx, signal.stk_cd)
        cur_prc = (quote or {}).get("cur_prc") or ctx.current_price(signal.stk_cd)
        sig_info = {
            "algo": signal.algo_code,
            "kind": signal.kind,
            "side": signal.side,
            "score": signal.score,
            "reason": (signal.reason or "")[:300],
            "order_type": {"0": "지정가", "3": "시장가", "6": "최유리지정가"}.get(
                signal.trde_tp, signal.trde_tp),
        }
        avg_down = None
        if signal.kind == KIND_AVG_DOWN:
            rate = ctx.holding_profit_rate(signal.stk_cd)
            state = ctx.position(signal.stk_cd)
            avg_down = {
                "drop_from_avg_pct": None if rate is None else round(float(rate), 2),
                "step": int(state.get("avg_down_count") or 0) + 1,
            }
        return build_payload(
            now=ctx.now,
            stk_cd=signal.stk_cd,
            stk_nm=signal.stk_nm,
            cur_prc=cur_prc,
            flu_rt=(quote or {}).get("flu_rt"),
            trde_qty=(quote or {}).get("trde_qty"),
            bars=bars,
            signal=sig_info,
            averaging_down=avg_down,
            extra=collect_context(ctx, signal),
        )

    @staticmethod
    def _bars(ctx, stk_cd: str) -> list[dict]:
        if ctx.market is not None:
            try:
                return list(ctx.market.daily_bars(stk_cd))[-MAX_BARS:]
            except Exception:  # noqa: BLE001 - 조회 실패는 DB 저장분으로 대체
                log.debug("일봉 조회 실패 %s", stk_cd, exc_info=True)
        try:
            return list(ctx.db.recent_bars(stk_cd, MAX_BARS))
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _quote(ctx, stk_cd: str) -> dict | None:
        if ctx.market is None:
            return None
        try:
            return ctx.market.quote(stk_cd)
        except Exception:  # noqa: BLE001
            log.debug("시세 조회 실패 %s", stk_cd, exc_info=True)
            return None

    @staticmethod
    def _anthropic_cfg(ctx):
        cfg = getattr(ctx, "anthropic_cfg", None)
        if cfg is None:
            raise ClaudeError("Anthropic 설정이 없습니다([anthropic] apikey_file)", infra=False)
        return cfg

    # -- 캐시 ----------------------------------------------------------- #
    def _cache_get(self, key) -> tuple[bool, str] | None:
        ttl = self.cache_sec
        if ttl <= 0:
            return None
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is None:
                return None
            ts, ok, detail = hit
            if (time.monotonic() - ts) >= ttl:
                _CACHE.pop(key, None)
                return None
            return ok, detail

    def _cache_put(self, key, ok: bool, detail: str) -> None:
        if self.cache_sec <= 0:
            return
        with _CACHE_LOCK:
            _CACHE[key] = (time.monotonic(), ok, detail)

    # -- 일 호출 상한 --------------------------------------------------- #
    @staticmethod
    def _calls_today(ctx) -> int | None:
        try:
            return int(ctx.db.llm_usage_today().get("calls", 0))
        except Exception:  # noqa: BLE001 - 조회 실패 시 상한 판정을 생략(다른 한도가 방어)
            log.debug("Claude 당일 호출수 조회 실패", exc_info=True)
            return None

    @staticmethod
    def _log_usage(ctx) -> None:
        try:
            u = ctx.db.llm_usage_today()
            ctx.db.log_event("INFO", "algo",
                             f"Claude 검토 당일 누적: 호출 {u['calls']}회, "
                             f"입력 {u['input_tokens']:,} 토큰, 출력 {u['output_tokens']:,} 토큰")
        except Exception:  # noqa: BLE001
            log.debug("Claude 사용량 기록 실패", exc_info=True)

    # -- 기록 ----------------------------------------------------------- #
    def _log_decision(self, ctx, signal: Signal, *, decision: str, final: str,
                      confidence, reasons: str, risk_flags: str, payload: dict | None,
                      from_cache: bool = False, model: str | None = None,
                      latency_ms: int | None = None, input_tokens: int | None = None,
                      output_tokens: int | None = None, error_msg: str | None = None) -> None:
        summary = None
        if payload is not None:
            try:
                summary = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:INPUT_SUMMARY_MAX]
            except (TypeError, ValueError):
                summary = None
        try:
            ctx.db.insert_llm_decision(
                run_id=ctx.run_id, stk_cd=signal.stk_cd, stk_nm=signal.stk_nm,
                source_algo=signal.algo_code, side=signal.side, model=model or self.model,
                decision=decision, final_action=final, confidence=confidence,
                reasons=(reasons or "")[:1000] or None,
                risk_flags=(risk_flags or "")[:300] or None,
                input_summary=summary, from_cache=1 if from_cache else 0,
                latency_ms=latency_ms, input_tokens=input_tokens, output_tokens=output_tokens,
                error_msg=(error_msg or "")[:255] or None, order_id=None)
        except Exception:  # noqa: BLE001 - 기록 실패가 판단을 바꾸지 않게
            log.debug("llm_decision_log 기록 실패", exc_info=True)
