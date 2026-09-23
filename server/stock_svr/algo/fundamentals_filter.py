"""fundamentals_filter — DART 재무분석(PER·PBR·ROE·부채비율) 기준으로 **신규 매수** 를 거르는 필터.

* `company_valuation_daily`(기업 재무분석 — DART OpenAPI + Claude, 매매와 완전 분리된 참고 리포트로
  이미 구현된 기능)의 **최신 평가일 행**을 읽어 개별 기준값(PER 상한 / PBR 상한 / ROE 하한 /
  부채비율 상한)을 모두 통과해야 매수 신호를 통과시킨다.
* 판정 방식은 종합 점수제가 아니라 **개별 기준값 통과제**(하나라도 위반하면 그 즉시 차단).
* 재무데이터가 아직 없거나(행 없음) 오래됐으면(`stale_days`) **매수를 차단**한다
  (fail-closed — `universe_filter` 의 종목마스터 stale 처리와 같은 철학).
* 지표 값이 NULL(예: 적자라 PER 계산 불가)이어도 그 지표는 위반으로 간주해 차단한다
  (안전 우선 원칙을 판정 전 구간에 일관 적용).
* 매도·손절·청산 신호에는 **절대 관여하지 않는다**(코드·테스트로 강제).
* 지표 토글을 전부 끄면(`use_per`~`use_debt` 모두 0) ma_cross_filter 의 단기>=장기 무력화와
  동일한 관용적 처리로 필터를 사실상 비활성화한다(DB 조회 없이 그대로 통과 + note 기록).
* "데이터 없음/오래됨"으로 차단할 때는 그 종목을 `services.fundamentals_fetch.enqueue()`
  로 온디맨드 재수집 큐(`company_fetch_request`)에 넣는다 — **DART 는 여기서 절대 호출하지
  않는다**(빠른 INSERT 하나뿐). 실제 수집은 엔진의 `FetchRequestWorker` 가 백그라운드에서
  그 종목 1개만 처리한다(임계값 위반 차단은 재수집 대상이 아니다 - `needs_fetch()` 참고).

파라미터(seed.sql): use_per, per_max, use_pbr, pbr_max, use_roe, roe_min, use_debt,
debt_ratio_max, stale_days, apply_to
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..services.fundamentals_fetch import enqueue as enqueue_fetch
from .base import KIND_AVG_DOWN, KIND_ENTRY, Algorithm, Signal
from .registry import register

log = logging.getLogger(__name__)

CODE = "fundamentals_filter"

APPLY_ENTRY = "entry"
APPLY_ENTRY_AND_AVG = "entry_and_avg"

__all__ = ["CODE", "FundamentalsFilter", "FundamentalsOptions", "age_days", "check_indicators",
           "check_valuation", "needs_fetch"]


# ====================================================================== #
# 파라미터
# ====================================================================== #
@dataclass(frozen=True)
class FundamentalsOptions:
    """폼/DB 어느 쪽에서 왔든 동일하게 쓰는 재무 건전성 조건."""

    use_per: bool = True
    per_max: Decimal = Decimal("25")
    use_pbr: bool = True
    pbr_max: Decimal = Decimal("3")
    use_roe: bool = True
    roe_min: Decimal = Decimal("5")
    use_debt: bool = True
    debt_ratio_max: Decimal = Decimal("200")
    apply_to: str = APPLY_ENTRY
    stale_days: int = 10

    @classmethod
    def from_params(cls, params) -> "FundamentalsOptions":
        return cls(
            use_per=params.bool("use_per", True),
            per_max=params.dec("per_max", 25),
            use_pbr=params.bool("use_pbr", True),
            pbr_max=params.dec("pbr_max", 3),
            use_roe=params.bool("use_roe", True),
            roe_min=params.dec("roe_min", 5),
            use_debt=params.bool("use_debt", True),
            debt_ratio_max=params.dec("debt_ratio_max", 200),
            apply_to=params.str("apply_to", APPLY_ENTRY) or APPLY_ENTRY,
            stale_days=params.int("stale_days", 10),
        )

    @property
    def applies_to_avg_down(self) -> bool:
        return self.apply_to == APPLY_ENTRY_AND_AVG

    @property
    def any_enabled(self) -> bool:
        """지표 토글이 하나라도 켜져 있는가. 모두 꺼졌으면 필터를 무력화한다."""
        return self.use_per or self.use_pbr or self.use_roe or self.use_debt


# ====================================================================== #
# 판정 (순수 함수 — DB 접근 없음)
# ====================================================================== #
def _fmt(val: Decimal) -> str:
    s = format(val, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def age_days(dt, now: _dt.datetime | _dt.date | None = None) -> int | None:
    """평가일(`dt`) 기준 경과 일수. 판단할 수 없으면 None."""
    if isinstance(dt, _dt.datetime):
        base = dt.date()
    elif isinstance(dt, _dt.date):
        base = dt
    else:
        return None
    ref = now or _dt.datetime.now()
    ref_date = ref.date() if isinstance(ref, _dt.datetime) else ref
    if not isinstance(ref_date, _dt.date):
        return None
    return max(0, (ref_date - base).days)


def check_indicators(row: dict, opts: FundamentalsOptions) -> tuple[bool, str]:
    """재무데이터 행(신선함은 이미 확인됨)이 켜진 지표를 모두 통과하는지.

    개별 기준값 통과제: PER → PBR → ROE → 부채비율 순서로 검사해 첫 위반에서 차단.
    값이 NULL 이면(해당 지표 계산 불가) 그 지표는 위반으로 간주한다(안전 우선).
    """
    if opts.use_per:
        per = _to_decimal(row.get("per"))
        if per is None:
            return False, "PER 계산 불가(적자 등) - 차단(안전 우선)"
        if per > opts.per_max:
            return False, f"PER {_fmt(per)}배 > 상한 {_fmt(opts.per_max)}배"
    if opts.use_pbr:
        pbr = _to_decimal(row.get("pbr"))
        if pbr is None:
            return False, "PBR 계산 불가(적자 등) - 차단(안전 우선)"
        if pbr > opts.pbr_max:
            return False, f"PBR {_fmt(pbr)}배 > 상한 {_fmt(opts.pbr_max)}배"
    if opts.use_roe:
        roe = _to_decimal(row.get("roe"))
        if roe is None:
            return False, "ROE 계산 불가(적자 등) - 차단(안전 우선)"
        if roe < opts.roe_min:
            return False, f"ROE {_fmt(roe)}% < 하한 {_fmt(opts.roe_min)}%"
    if opts.use_debt:
        debt = _to_decimal(row.get("debt_ratio"))
        if debt is None:
            return False, "부채비율 계산 불가(적자 등) - 차단(안전 우선)"
        if debt > opts.debt_ratio_max:
            return False, f"부채비율 {_fmt(debt)}% > 상한 {_fmt(opts.debt_ratio_max)}%"
    return True, ""


def needs_fetch(row: dict | None, opts: FundamentalsOptions,
                now: _dt.datetime | None = None) -> bool:
    """차단 사유가 온디맨드 재수집 대상("데이터 없음" 또는 "stale")인가.

    `check_valuation` 의 앞 두 분기(행 없음 / stale)만 그대로 재현한다.
    임계값 위반(행은 있고 최신인데 기준 초과)은 **재수집 대상이 아니다** — 이미
    최신 데이터로 정당하게 차단된 것이라 다시 받아도 결과가 바뀌지 않는다.
    기준일을 확인할 수 없는 경우(`age is None`)도 애매하므로 대상에서 뺀다.
    """
    if row is None:
        return True
    age = age_days(row.get("dt"), now)
    if age is None:
        return False
    return age > opts.stale_days


def check_valuation(row: dict | None, opts: FundamentalsOptions,
                    now: _dt.datetime | None = None) -> tuple[bool, str]:
    """평가행 + 옵션으로 통과 여부를 판단하는 순수 함수(DB 접근 없음).

    1. 행이 없으면 차단(데이터 없음).
    2. 행의 dt 가 stale_days 보다 오래되면 차단.
    3. 켜진 지표를 모두 통과해야 통과(`check_indicators`).
    """
    if row is None:
        return False, "재무데이터 없음 - 차단(안전 우선)"
    dt = row.get("dt")
    age = age_days(dt, now)
    if age is None:
        return False, f"재무데이터 기준일 확인 불가({dt}) - 차단(안전 우선)"
    if age > opts.stale_days:
        return False, f"재무데이터 {age}일 전({dt}) - 허용 {opts.stale_days}일 초과"
    return check_indicators(row, opts)


# ====================================================================== #
@register
class FundamentalsFilter(Algorithm):
    code = CODE
    role = "filter"
    name = "재무 건전성 필터(PER·PBR·ROE·부채비율)"

    # ------------------------------------------------------------------ #
    def filter_signals(self, ctx, signals: list[Signal]) -> list[Signal]:
        if not signals:
            return signals
        opts = FundamentalsOptions.from_params(self.params)
        if not opts.any_enabled:
            # ma_cross_filter 의 단기>=장기 무력화와 동일한 관용적 처리: DB 조회 없이 전부 통과
            ctx.note(f"{self.code}: 지표 토글이 모두 꺼짐 - 필터 사실상 비활성")
            return signals
        if not any(self._applies(s, opts) for s in signals):
            return signals        # 매도·손절·청산뿐이면 DB 조회조차 하지 않는다

        kept: list[Signal] = []
        for sig in signals:
            if not self._applies(sig, opts):
                kept.append(sig)
                continue
            ok, reason = self._check(ctx, sig, opts)
            if not ok:
                self._block(ctx, sig, reason)
                continue
            kept.append(sig)
        return kept

    # ------------------------------------------------------------------ #
    def _applies(self, sig: Signal, opts: FundamentalsOptions) -> bool:
        """매수 신호에만, 그것도 apply_to 가 허용하는 종류에만 관여한다.

        SELL(손절·청산·익절 포함)은 어떤 설정에서도 대상이 아니다.
        """
        if sig.side != "BUY":
            return False
        if sig.kind == KIND_ENTRY:
            return True
        if sig.kind == KIND_AVG_DOWN:
            return opts.applies_to_avg_down
        return False

    def _check(self, ctx, sig: Signal, opts: FundamentalsOptions) -> tuple[bool, str]:
        stk_cd = sig.stk_cd
        try:
            row = ctx.db.latest_company_valuation(stk_cd)
        except Exception as exc:  # noqa: BLE001 - 조회 실패는 판단 불가 → 차단(fail-closed)
            log.warning("재무데이터 조회 실패 %s", stk_cd, exc_info=True)
            return False, f"재무데이터 조회 실패({type(exc).__name__}) - 차단(안전 우선)"
        ok, reason = check_valuation(row, opts, ctx.now)
        if not ok and needs_fetch(row, opts, ctx.now):
            # 데이터 없음/오래됨으로 차단됐을 때만 그 종목을 온디맨드 재수집 큐에 넣는다.
            # **DART 를 여기서 직접 호출하지 않는다** - 빠른 INSERT 하나뿐이고, 실패해도
            # (dedupe 조회 포함) 이 필터의 차단 판정에는 절대 영향을 주지 않는다.
            self._enqueue_fetch(ctx, stk_cd, sig.stk_nm)
        return ok, reason

    def _enqueue_fetch(self, ctx, stk_cd: str, stk_nm: str | None) -> None:
        try:
            enqueue_fetch(ctx.db, stk_cd, stk_nm, source=self.code)
        except Exception:  # noqa: BLE001 - 필터는 어떤 경우에도 이 호출 때문에 죽으면 안 된다
            log.debug("%s: 온디맨드 재수집 요청 등록 실패(무시) %s", self.code, stk_cd, exc_info=True)

    def _block(self, ctx, sig: Signal, detail: str) -> None:
        log.info("%s 차단: %s %s", self.code, sig.stk_cd, detail)
        ctx.note(f"{self.code}: {sig.stk_cd} {detail}")
        try:
            ctx.db.insert_signal(ctx.run_id, self.code, sig.stk_cd, sig.stk_nm, "BLOCK",
                                 None, f"{sig.algo_code} 신호 차단 - {detail}")
        except Exception:  # noqa: BLE001
            log.debug("BLOCK 신호 기록 실패", exc_info=True)
