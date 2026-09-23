"""universe_filter — 시가총액·주가 기준으로 **신규 매수 대상 종목(유니버스)** 을 제한하는 필터.

* `stock_master`(ka10099 로 받은 종목마스터)의 상장주식수 × 전일종가로 시가총액을 구해
  코스피/코스닥/ETF 시총 순위를 만들고, 순위·시총 하한·주가 범위를 벗어나는 **매수 신호만** 차단한다.
* 매도·손절·청산 신호에는 **절대 관여하지 않는다**(코드·테스트로 강제).
* 순위는 종목마스터 갱신(`updated_at`) 이 바뀌기 전까지 메모리에 캐시한다(사이클마다 재계산 금지).
* 마스터가 비었거나 오래됐거나 조회에 실패하면 **신규 매수를 차단**한다(fail-closed).

파라미터(seed.sql): use_kospi, use_kosdaq, use_etf, rank_scope, top_n, min_market_cap_eok,
min_price, max_price, exclude_preferred, exclude_spac, exclude_warning, apply_to, stale_days
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import threading
from dataclasses import dataclass, field

from .base import KIND_AVG_DOWN, KIND_ENTRY, Algorithm, Signal
from .registry import register

log = logging.getLogger(__name__)

CODE = "universe_filter"

MARKET_KOSPI = "0"
MARKET_KOSDAQ = "10"
MARKET_ETF = "8"            # 국내 상장 ETF (금현물·ETN·리츠 등 다른 코드는 대상 아님)
TARGET_MARKETS = (MARKET_KOSPI, MARKET_KOSDAQ, MARKET_ETF)
MARKET_LABEL = {MARKET_KOSPI: "코스피", MARKET_KOSDAQ: "코스닥", MARKET_ETF: "ETF"}

SCOPE_PER_MARKET = "per_market"
SCOPE_COMBINED = "combined"
COMBINED_KEY = "combined"
COMBINED_LABEL = "코스피+코스닥"      # 선택 시장이 없을 때만 쓰는 표시용 기본값

APPLY_ENTRY = "entry"
APPLY_ENTRY_AND_AVG = "entry_and_avg"

EOK = 100_000_000          # 1억원

# 우선주 이름 접미: (숫자)우(영문 1자)((전환)) 형태. 오탐(미래에셋대우 등)을 막기 위해
# **접미를 뗀 보통주 이름이 같은 마스터에 있을 때만** 우선주로 본다.
_PREFERRED_SUFFIX_RE = re.compile(r"\d*우[A-Z]?(?:\([^()]*\))?$")

# stock_master.state 에 이 단어가 있으면 '경고 종목'으로 본다
WARNING_STATE_WORDS = ("관리종목", "거래정지", "정리매매", "상장폐지", "환기종목",
                       "투자주의", "투자경고", "투자위험", "단기과열")

__all__ = [
    "CODE", "EOK", "MARKET_LABEL", "UniverseFilter", "UniverseEntry", "UniverseOptions",
    "Universe", "build_universe", "check_entry", "load_universe", "clear_universe_cache",
    "is_preferred_name", "is_preferred", "master_age_days",
]


# ====================================================================== #
# 파라미터
# ====================================================================== #
@dataclass(frozen=True)
class UniverseOptions:
    """폼/DB 어느 쪽에서 왔든 동일하게 쓰는 유니버스 조건."""

    use_kospi: bool = True
    use_kosdaq: bool = True
    use_etf: bool = True
    rank_scope: str = SCOPE_PER_MARKET
    top_n: int = 100
    min_market_cap_eok: int = 0
    min_price: int = 50000
    max_price: int = 0
    exclude_preferred: bool = True
    exclude_spac: bool = True
    exclude_warning: bool = True
    apply_to: str = APPLY_ENTRY
    stale_days: int = 5

    @classmethod
    def from_params(cls, params) -> "UniverseOptions":
        """`ParamSet`(DB 저장값 또는 UI 폼 정규화값)에서 만든다."""
        return cls(
            use_kospi=params.bool("use_kospi", True),
            use_kosdaq=params.bool("use_kosdaq", True),
            use_etf=params.bool("use_etf", True),
            rank_scope=params.str("rank_scope", SCOPE_PER_MARKET) or SCOPE_PER_MARKET,
            top_n=params.int("top_n", 100),
            min_market_cap_eok=params.int("min_market_cap_eok", 0),
            min_price=params.int("min_price", 0),
            max_price=params.int("max_price", 0),
            exclude_preferred=params.bool("exclude_preferred", True),
            exclude_spac=params.bool("exclude_spac", True),
            exclude_warning=params.bool("exclude_warning", True),
            apply_to=params.str("apply_to", APPLY_ENTRY) or APPLY_ENTRY,
            stale_days=params.int("stale_days", 5),
        )

    # -- 편의 --------------------------------------------------------- #
    @property
    def markets(self) -> tuple[str, ...]:
        out = []
        if self.use_kospi:
            out.append(MARKET_KOSPI)
        if self.use_kosdaq:
            out.append(MARKET_KOSDAQ)
        if self.use_etf:
            out.append(MARKET_ETF)
        return tuple(out)

    @property
    def markets_text(self) -> str:
        names = [MARKET_LABEL[m] for m in self.markets]
        return "+".join(names) if names else "(없음)"

    @property
    def combined_label(self) -> str:
        """합산 순위 스코프 라벨(선택된 시장 이름). 예: '코스피+코스닥+ETF'."""
        return self.markets_text if self.markets else COMBINED_LABEL

    @property
    def min_market_cap_won(self) -> int:
        return max(0, int(self.min_market_cap_eok)) * EOK

    @property
    def applies_to_avg_down(self) -> bool:
        return self.apply_to == APPLY_ENTRY_AND_AVG

    def scope_label(self, market_code: str) -> str:
        if self.rank_scope == SCOPE_COMBINED:
            return self.combined_label
        return MARKET_LABEL.get(market_code, market_code)

    def errors(self) -> list[str]:
        """파라미터 정의(min/max)만으로는 표현할 수 없는 제약 검증."""
        out: list[str] = []
        if not self.markets:
            out.append("코스피 포함·코스닥 포함·ETF 포함 중 최소 하나는 켜야 합니다.")
        if self.max_price > 0 and self.max_price < self.min_price:
            out.append(f"최대 주가({self.max_price:,}원)는 최소 주가"
                       f"({self.min_price:,}원) 이상이어야 합니다.")
        if self.top_n <= 0:
            out.append("시가총액 상위 N 은 1 이상이어야 합니다.")
        return out

    def signature(self) -> tuple:
        """순위 캐시 키(순위 산출에 영향을 주는 값만)."""
        return (self.use_kospi, self.use_kosdaq, self.use_etf, self.rank_scope, self.top_n,
                self.min_market_cap_eok, self.min_price, self.max_price,
                self.exclude_preferred, self.exclude_spac, self.exclude_warning)


# ====================================================================== #
# 유니버스 계산 (순수 함수)
# ====================================================================== #
@dataclass
class UniverseEntry:
    stk_cd: str
    stk_nm: str
    market_code: str
    list_count: int = 0
    last_price: int = 0
    market_cap: int = 0
    rank: int | None = None
    excluded: str = ""          # 순위 대상에서 빠진 사유("" 이면 순위 대상)
    passed: bool = False        # 전일종가 기준 통과 여부(미리보기용)
    reason: str = ""            # 통과하지 못한 사유

    @property
    def market_label(self) -> str:
        return MARKET_LABEL.get(self.market_code, self.market_code or "-")

    @property
    def cap_eok(self) -> int:
        return int(self.market_cap // EOK)


@dataclass
class Universe:
    entries: dict[str, UniverseEntry] = field(default_factory=dict)
    ranked: list[UniverseEntry] = field(default_factory=list)
    updated_at: _dt.datetime | None = None
    source_rows: int = 0
    passed_by_market: dict[str, int] = field(default_factory=dict)
    cutoff_by_scope: dict[str, int] = field(default_factory=dict)

    @property
    def passed_total(self) -> int:
        return sum(self.passed_by_market.values())

    @property
    def passed_entries(self) -> list[UniverseEntry]:
        return [e for e in self.ranked if e.passed]


def is_preferred_name(name: str, base_names: set[str]) -> bool:
    """이름만으로 보는 우선주 판정(보수적).

    접미가 우선주 형태이고 **접미를 뗀 보통주 이름이 실제로 상장돼 있을 때만** True.
    '삼성전자우'→True('삼성전자' 존재), '우리금융지주'·'우진'·'미래에셋대우'→False.
    """
    name = (name or "").strip()
    m = _PREFERRED_SUFFIX_RE.search(name)
    if not m or m.start() == 0:
        return False
    base = name[:m.start()].strip()
    if len(base) < 2:
        return False
    return base in base_names


def is_preferred(name: str, stk_cd: str, base_names: set[str], codes: set[str]) -> bool:
    """우선주 판정. 이름 접미가 우선주 형태이면서 **보통주가 함께 상장돼 있을 때만** True.

    보통주는 두 가지 근거 중 하나로 확인한다(둘 다 없으면 오탐으로 보고 제외하지 않는다).
      ① 접미를 뗀 이름이 그대로 상장돼 있다 ('삼성전자우' → '삼성전자')
      ② 종목코드 끝자리가 0 이 아니고, 끝자리를 0 으로 바꾼 코드가 상장돼 있다
         ('남선알미우' 008355 → '남선알미늄' 008350 · 이름이 줄여진 우선주 대응)
    '에코글로우'·'이오플로우'(끝자리 0)·'우리금융지주'·'미래에셋대우' 는 어느 쪽에도 걸리지 않는다.
    """
    name = (name or "").strip()
    m = _PREFERRED_SUFFIX_RE.search(name)
    if not m or m.start() == 0:
        return False
    if is_preferred_name(name, base_names):
        return True
    code = (stk_cd or "").strip()
    if len(code) < 2 or code[-1] == "0":
        return False
    return (code[:-1] + "0") in codes


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _exclude_reason(row: dict, opts: UniverseOptions, base_names: set[str],
                    codes: set[str]) -> str:
    name = str(row.get("stk_nm") or "").strip()
    if opts.exclude_spac and "스팩" in name:
        return "스팩"
    if opts.exclude_preferred and is_preferred(name, str(row.get("stk_cd") or ""),
                                               base_names, codes):
        return "우선주"
    if opts.exclude_warning:
        state = str(row.get("state") or "")
        for word in WARNING_STATE_WORDS:
            if word in state:
                return word
        warn = str(row.get("order_warning") or "0").strip()
        if warn and warn != "0":
            return f"투자유의(order_warning={warn})"
    return ""


def build_universe(rows: list[dict], opts: UniverseOptions,
                   updated_at: _dt.datetime | None = None) -> Universe:
    """종목마스터 행 목록으로 시총 순위·통과 여부를 계산한다(DB·API 접근 없음)."""
    uni = Universe(updated_at=updated_at, source_rows=len(rows))
    base_names = {str(r.get("stk_nm") or "").strip() for r in rows}
    codes = {str(r.get("stk_cd") or "").strip() for r in rows}
    wanted = set(opts.markets)

    for row in rows:
        code = str(row.get("stk_cd") or "").strip()
        market = str(row.get("market_code") or "").strip()
        if not code or market not in TARGET_MARKETS:
            continue
        entry = UniverseEntry(
            stk_cd=code,
            stk_nm=str(row.get("stk_nm") or "").strip(),
            market_code=market,
            list_count=_to_int(row.get("list_count")),
            last_price=_to_int(row.get("last_price")),
        )
        entry.market_cap = max(0, entry.list_count) * max(0, entry.last_price)
        if market not in wanted:
            entry.excluded = f"대상 시장 아님({entry.market_label})"
        elif entry.list_count <= 0 or entry.last_price <= 0:
            entry.excluded = "시가총액 계산 불가(상장주식수·전일종가 없음)"
        else:
            entry.excluded = _exclude_reason(row, opts, base_names, codes)
        uni.entries[code] = entry

    # 제외 규칙을 적용한 뒤 시가총액 내림차순(동률은 종목코드 오름차순으로 확정)
    ranked = [e for e in uni.entries.values() if not e.excluded]
    ranked.sort(key=lambda e: (-e.market_cap, e.stk_cd))
    if opts.rank_scope == SCOPE_COMBINED:
        for i, e in enumerate(ranked, start=1):
            e.rank = i
    else:
        seq: dict[str, int] = {}
        for e in ranked:
            seq[e.market_code] = seq.get(e.market_code, 0) + 1
            e.rank = seq[e.market_code]
    uni.ranked = ranked

    for e in uni.entries.values():
        ok, reason = check_entry(e, opts, e.last_price)
        e.passed, e.reason = ok, reason
        if not ok:
            continue
        uni.passed_by_market[e.market_code] = uni.passed_by_market.get(e.market_code, 0) + 1
        key = COMBINED_KEY if opts.rank_scope == SCOPE_COMBINED else e.market_code
        cur = uni.cutoff_by_scope.get(key)
        if cur is None or e.market_cap < cur:
            uni.cutoff_by_scope[key] = e.market_cap
    return uni


def check_entry(entry: UniverseEntry, opts: UniverseOptions,
                price: int | None = None) -> tuple[bool, str]:
    """이 종목이 매수 대상인지. 실패하면 **구체적인 사유**를 함께 돌려준다."""
    if entry.excluded:
        return False, f"유니버스 제외: {entry.excluded}"
    if entry.rank is None:
        return False, "유니버스 제외: 시총 순위 산출 실패"
    if entry.rank > opts.top_n:
        return False, (f"유니버스 제외: {opts.scope_label(entry.market_code)} 시총순위 "
                       f"{entry.rank}위 > {opts.top_n}")
    min_cap = opts.min_market_cap_won
    if min_cap > 0 and entry.market_cap < min_cap:
        return False, (f"유니버스 제외: 시가총액 {entry.cap_eok:,}억원 < 최소 "
                       f"{opts.min_market_cap_eok:,}억원")
    p = _to_int(price) or entry.last_price
    if p <= 0:
        return False, "유니버스 제외: 주가 확인 불가"
    if opts.min_price > 0 and p < opts.min_price:
        return False, f"주가 {p:,}원 < 최소 {opts.min_price:,}원"
    if opts.max_price > 0 and p > opts.max_price:
        return False, f"주가 {p:,}원 > 최대 {opts.max_price:,}원"
    return True, ""


# ====================================================================== #
# 순위 캐시 (종목마스터 갱신 시 무효화)
# ====================================================================== #
_CACHE_LOCK = threading.Lock()
_CACHE: dict[tuple, tuple[tuple, Universe]] = {}
_CACHE_MAX = 8


def clear_universe_cache() -> None:
    """종목마스터가 갱신되면 호출한다(MarketService.sync_stock_master / 테스트)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def master_age_days(updated_at, now: _dt.datetime | None = None) -> int | None:
    """종목마스터 갱신 후 경과 일수. 판단할 수 없으면 None."""
    if not isinstance(updated_at, _dt.datetime):
        if isinstance(updated_at, _dt.date):
            base = updated_at
        else:
            return None
    else:
        base = updated_at.date()
    ref = (now or _dt.datetime.now()).date()
    return max(0, (ref - base).days)


def load_universe(db, opts: UniverseOptions,
                  now: _dt.datetime | None = None) -> tuple[Universe | None, str]:
    """유니버스를 (캐시 포함) 준비한다. 실패하면 (None, 차단 사유)."""
    errs = opts.errors()
    if errs:
        return None, "파라미터 오류 - " + " ".join(errs)
    try:
        stats = db.stock_master_stats() or {}
    except Exception as exc:  # noqa: BLE001 - 판단 불가 → 신규 매수 차단(fail-closed)
        log.warning("종목마스터 상태 조회 실패", exc_info=True)
        return None, f"종목마스터 조회 실패({type(exc).__name__}) - 신규 매수 차단"
    count = _to_int(stats.get("count"))
    updated_at = stats.get("updated_at")
    if count <= 0:
        return None, "종목마스터가 비어 있음 - 신규 매수 차단"
    age = master_age_days(updated_at, now)
    if age is None:
        return None, "종목마스터 갱신 시각 확인 불가 - 신규 매수 차단"
    if age > opts.stale_days:
        return None, (f"종목마스터가 {age}일 전 갱신(허용 {opts.stale_days}일) "
                      f"- 신규 매수 차단")

    stamp = (updated_at, count)
    key = opts.signature()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1], ""

    try:
        rows = db.stock_master_universe(TARGET_MARKETS)
    except Exception as exc:  # noqa: BLE001
        log.warning("종목마스터 조회 실패", exc_info=True)
        return None, f"종목마스터 조회 실패({type(exc).__name__}) - 신규 매수 차단"
    if not rows:
        return None, "종목마스터에 코스피·코스닥·ETF 종목이 없음 - 신규 매수 차단"

    uni = build_universe(rows, opts, updated_at=updated_at)
    if not uni.ranked:
        return None, f"유니버스 대상 종목이 없음({opts.markets_text}) - 신규 매수 차단"
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = (stamp, uni)
    return uni, ""


# ====================================================================== #
@register
class UniverseFilter(Algorithm):
    code = CODE
    role = "filter"
    name = "종목 유니버스 필터(시가총액·주가)"

    @staticmethod
    def validate_params(params) -> list[str]:
        return UniverseOptions.from_params(params).errors()

    # ------------------------------------------------------------------ #
    def filter_signals(self, ctx, signals: list[Signal]) -> list[Signal]:
        if not signals:
            return signals
        opts = UniverseOptions.from_params(self.params)
        if not any(self._applies(s, opts) for s in signals):
            return signals        # 매도·손절·청산뿐이면 DB 조회조차 하지 않는다

        uni, err = load_universe(ctx.db, opts, ctx.now)
        if err:
            ctx.note(f"{self.code}: {err}")
        kept: list[Signal] = []
        for sig in signals:
            if not self._applies(sig, opts):
                kept.append(sig)
                continue
            if uni is None:
                self._block(ctx, sig, err or "유니버스 확인 불가 - 신규 매수 차단")
                continue
            entry = uni.entries.get(sig.stk_cd)
            if entry is None:
                self._block(ctx, sig,
                            f"유니버스 제외: 종목마스터에 없음(대상 {opts.markets_text})")
                continue
            ok, reason = check_entry(entry, opts, self._signal_price(ctx, sig))
            if not ok:
                self._block(ctx, sig, reason)
                continue
            kept.append(sig)
        return kept

    # ------------------------------------------------------------------ #
    def _applies(self, sig: Signal, opts: UniverseOptions) -> bool:
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

    def _signal_price(self, ctx, sig: Signal) -> int:
        """신호 시점 주가. 없으면 0 을 돌려 전일종가(last_price)로 판단하게 한다."""
        for raw in (sig.meta.get("cur_prc"), sig.price):
            price = _to_int(raw)
            if price > 0:
                return price
        # 추가 API 호출 없이 보유종목 현재가만 본다
        price = ctx.current_price(sig.stk_cd, fallback_quote=False)
        return _to_int(price)

    def _block(self, ctx, sig: Signal, detail: str) -> None:
        log.info("%s 차단: %s %s", self.code, sig.stk_cd, detail)
        ctx.note(f"{self.code}: {sig.stk_cd} {detail}")
        try:
            ctx.db.insert_signal(ctx.run_id, self.code, sig.stk_cd, sig.stk_nm, "BLOCK",
                                 None, f"{sig.algo_code} 신호 차단 - {detail}")
        except Exception:  # noqa: BLE001
            log.debug("BLOCK 신호 기록 실패", exc_info=True)
