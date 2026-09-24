"""기업 재무분석 (DART OpenAPI + Claude) — **하루 1회, 읽기 전용 참고 리포트**.

    시총 상위 종목(ETF 제외) → ① DART 고유번호 매핑 → ② 분기/연간 재무제표 수집
                            → ③ 주가와 결합해 PER/PBR/ROE/부채비율 계산
                            → ④ Claude 가 그 숫자를 **해석만** 해서 리포트 작성

**매매와 완전히 분리되어 있다.**
* `algorithm` / `algorithm_selection` 에 등록하지 않는다(알고리즘이 아니다).
* `signal_log` / `orders` / Executor / risk_guard 어디에도 연결되지 않는다 — 신호를 만들지
  않으므로 연결할 것 자체가 없다.
* 주문 게이트(`order_enabled`·`trading_mode`·`real_trading_confirm`)를 **읽지도 쓰지도 않는다.**
  자동거래 ON/OFF 와 무관하게 엔진이 돌고 있으면 동작한다.
* 키움 API 를 추가로 호출하지 않는다 — 이미 DB 에 있는 `stock_master`(상장주식수·전일종가)와
  `price_daily`(최신 종가)만 읽는다.

비용/호출 제어
* 이미 저장된 (종목, 연도, 보고서코드) 조합은 **다시 조회하지 않는다**(확정된 분기 값은
  바뀌지 않는다). 강제 새로고침은 CLI(`--fundamentals-check --refresh`)에서만 가능하다.
* 처음 보는 종목만 5년치 전체를 받고, 그 뒤로는 **최근 공시 가능 분기**만 확인한다
  (아직 미공시면 DART 가 status='013' 을 주며 이것은 오류가 아니다).
* 한 번 실행에서 나가는 DART 호출 수와 Claude 리포트 건수에 상한이 있다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from ..algo.universe_filter import (
    MARKET_KOSDAQ,
    MARKET_KOSPI,
    SCOPE_PER_MARKET,
    UniverseOptions,
    load_universe,
)
from ..dart.client import (
    FS_DIVS,
    REPRT_ANNUAL,
    REPRT_CODES,
    REPRT_H1,
    REPRT_Q1,
    REPRT_Q3,
    DartClient,
    DartError,
    DartNoData,
)
from ..dart.parse import has_any_value, parse_financial_rows
from ..dart.valuation import REPRT_LABEL, compute_valuation, period_label
from ..llm.client import ClaudeError
from ..llm.fundamental_prompt import (
    FUNDAMENTAL_OUTPUT_SCHEMA,
    REPORT_MAX_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    build_financial_payload,
    build_report_prompt,
    validate_fundamental_output,
)
from ..llm.prompt import OutputSchemaError
from ..util import is_weekday, mask_text, now_kst, today_kst

log = logging.getLogger(__name__)

# 로그·이벤트에서 이 기능을 가리키는 이름. **`algorithm.code` 가 아니다**(DB 에 등록하지 않는다).
CODE = "company_fundamentals"

# 엔진 루프가 이 서비스를 확인하는 주기(초). 다른 폴링 상수와 겹치지 않는 별도 값이다.
POLL_SEC = 1800

# 기본값 (system_setting 으로 덮어쓸 수 있다 — 없으면 이 값. 서버가 값을 **쓰지는 않는다**)
DEFAULT_TOP_N = 30              # 시총 상위 몇 종목을 대상으로 할지(시장별)
DEFAULT_YEARS = 5               # 몇 년치 재무제표를 받을지
DEFAULT_REPORT_LIMIT = 3        # 하루에 만들 Claude 리포트 건수 상한
DEFAULT_MAX_FETCH = 300         # 한 번 실행에서 나갈 DART 재무제표 호출 수 상한
DEFAULT_RUN_HOUR = 16           # 장마감 후 (housekeeping 과 같은 기준)
# 리포트는 길어서(수천 토큰) 비용을 고려해 sonnet 을 기본으로 둔다. llm.client.MODELS 만 허용.
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT_SEC = 180

# 이미 재무데이터가 있는 종목은 매일 이 개수만큼의 **최근 분기만** 확인한다
RECENT_PERIODS = 2

# 종목마스터가 오래돼도 참고 리포트는 만들 수 있다(매수 차단용 fail-closed 가 아니다)
MASTER_STALE_DAYS = 30

STATUS_OK = "ok"
STATUS_ERROR = "error"

# 보고서별 '공시가 나올 법한 시점'(월, 일). 분기보고서 45일·사업보고서 90일 법정기한에서
# 조금 앞당긴 값으로, 이 날짜부터 그 분기를 조회 대상에 넣는다(그 전에는 물어봐야 없다).
_PERIOD_OPEN = {
    REPRT_Q1: (5, 5),        # 1분기(3월말) → 5월 초
    REPRT_H1: (8, 4),        # 반기(6월말)  → 8월 초
    REPRT_Q3: (11, 4),       # 3분기(9월말) → 11월 초
    REPRT_ANNUAL: (3, 21),   # 사업보고서(12월말) → 다음 해 3월 하순
}

# 하루 1회 보장(프로세스 내 날짜 캐시)
_STATE_LOCK = threading.Lock()
_last_run_date: _dt.date | None = None


def reset_state() -> None:
    """'오늘 이미 실행함' 표시를 지운다(테스트/재설정용)."""
    global _last_run_date
    with _STATE_LOCK:
        _last_run_date = None


def last_run_date() -> _dt.date | None:
    with _STATE_LOCK:
        return _last_run_date


def _mark_run(day: _dt.date) -> None:
    global _last_run_date
    with _STATE_LOCK:
        _last_run_date = day


# ---------------------------------------------------------------------- #
def period_open_date(year: int, reprt_code: str) -> _dt.date | None:
    """그 보고서를 조회해 볼 만한 가장 이른 날짜. 사업보고서는 **다음 해** 봄이다."""
    spec = _PERIOD_OPEN.get(str(reprt_code))
    if spec is None:
        return None
    month, day = spec
    base_year = year + 1 if str(reprt_code) == REPRT_ANNUAL else year
    return _dt.date(base_year, month, day)


def open_periods(today: _dt.date, years: int) -> list[tuple[int, str]]:
    """최근 `years` 년 안에서 **이미 공시됐을 법한** (연도, 보고서코드) 목록(과거 → 최신)."""
    out: list[tuple[int, str]] = []
    for year in range(today.year - max(1, int(years)) + 1, today.year + 1):
        for reprt in REPRT_CODES:
            opened = period_open_date(year, reprt)
            if opened is not None and opened <= today:
                out.append((year, reprt))
    out.sort(key=lambda p: (period_open_date(p[0], p[1]) or _dt.date.min))
    return out


# ====================================================================== #
class FundamentalsOptions:
    """운영 파라미터. `system_setting` 에서 **읽기만** 한다(알고리즘 파라미터가 아니다)."""

    def __init__(self, db=None):
        self.top_n = self._int(db, "fundamentals_top_n", DEFAULT_TOP_N, 1, 500)
        self.years = self._int(db, "fundamentals_years", DEFAULT_YEARS, 1, 10)
        self.report_limit = self._int(db, "fundamentals_report_limit",
                                      DEFAULT_REPORT_LIMIT, 0, 50)
        self.max_fetch = self._int(db, "fundamentals_max_fetch", DEFAULT_MAX_FETCH, 0, 5000)
        self.run_hour = self._int(db, "fundamentals_run_hour", DEFAULT_RUN_HOUR, 0, 23)
        self.timeout_sec = float(self._int(db, "fundamentals_timeout_sec",
                                           DEFAULT_TIMEOUT_SEC, 30, 600))
        self.model = self._str(db, "fundamentals_model", DEFAULT_MODEL)

    @staticmethod
    def _int(db, key: str, default: int, low: int, high: int) -> int:
        raw = default
        if db is not None:
            try:
                raw = db.get_setting(key, str(default))
            except Exception:  # noqa: BLE001 - 설정 조회 실패는 기본값으로
                raw = default
        try:
            return max(low, min(high, int(str(raw).strip())))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _str(db, key: str, default: str) -> str:
        from ..llm.client import MODELS

        value = default
        if db is not None:
            try:
                value = str(db.get_setting(key, default) or default).strip()
            except Exception:  # noqa: BLE001
                value = default
        return value if value in MODELS else default

    @property
    def universe_options(self) -> UniverseOptions:
        """시총 상위 순위 계산 조건. **ETF 는 DART 재무제표가 없으므로 항상 제외**한다."""
        return UniverseOptions(
            use_kospi=True, use_kosdaq=True, use_etf=False,
            rank_scope=SCOPE_PER_MARKET, top_n=self.top_n,
            min_market_cap_eok=0, min_price=0, max_price=0,
            exclude_preferred=True, exclude_spac=True, exclude_warning=False,
            stale_days=MASTER_STALE_DAYS)


class FundamentalsResult:
    """실행 1회 결과(CLI 점검·로그용)."""

    def __init__(self):
        self.targets: list[dict] = []
        self.corp_code: dict = {}
        self.fetch = {"calls": 0, "saved": 0, "no_data": 0, "errors": 0, "skipped": 0}
        self.valuations: list[dict] = []
        self.reports: list[dict] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.errors: list[str] = []

    @property
    def status(self) -> str:
        return STATUS_ERROR if self.errors and not self.reports else STATUS_OK


# ====================================================================== #
class FundamentalsService:
    """DART 수집 + 밸류에이션 계산 + Claude 리포트. 신호·주문은 만들지 않는다."""

    def __init__(self, db, dart_cfg=None, anthropic_cfg=None, dart=None, client=None):
        self.db = db
        self.dart_cfg = dart_cfg
        self.anthropic_cfg = anthropic_cfg
        self._dart = dart            # 테스트/CLI 에서 주입 가능
        self._client = client
        self.opts = FundamentalsOptions(db)

    # ------------------------------------------------------------------ #
    @property
    def configured(self) -> bool:
        if self._dart is not None:
            return True
        return bool(self.dart_cfg is not None and getattr(self.dart_cfg, "configured", False))

    def dart(self) -> DartClient:
        if self._dart is None:
            if self.dart_cfg is None:
                raise DartError("[dart] 설정이 없습니다(apikey_file)")
            self._dart = DartClient(self.dart_cfg)
        return self._dart

    def claude(self):
        if self._client is None:
            from ..algo.claude_advisor import get_client

            if self.anthropic_cfg is None:
                raise ClaudeError("Anthropic 설정이 없습니다([anthropic] apikey_file)",
                                  infra=False)
            self._client = get_client(self.anthropic_cfg)
        return self._client

    def close(self) -> None:
        """DART HTTP 세션만 닫는다(Claude 클라이언트는 공용이라 건드리지 않는다)."""
        if self._dart is not None:
            try:
                self._dart.close()
            except Exception:  # noqa: BLE001
                pass

    # ================================================================== #
    # 대상 종목 — universe_filter 의 시총 상위 순위 결과를 재사용한다
    # ================================================================== #
    def targets(self, now: _dt.datetime | None = None) -> list[dict]:
        """시총 상위 종목(코스피/코스닥, ETF·우선주·스팩 제외). 실패하면 빈 목록."""
        uni, err = load_universe(self.db, self.opts.universe_options, now or now_kst())
        if uni is None:
            log.warning("%s: 대상 종목을 만들 수 없습니다 - %s", CODE, err)
            return []
        out = []
        for entry in uni.passed_entries:
            if entry.market_code not in (MARKET_KOSPI, MARKET_KOSDAQ):
                continue
            out.append({
                "stk_cd": entry.stk_cd, "stk_nm": entry.stk_nm,
                "market_code": entry.market_code, "market_label": entry.market_label,
                "rank": entry.rank, "market_cap": entry.market_cap,
                "list_count": entry.list_count, "last_price": entry.last_price,
            })
        out.sort(key=lambda t: (-(t["market_cap"] or 0), t["stk_cd"]))
        return out

    # ================================================================== #
    # ② 재무제표 수집
    # ================================================================== #
    def fetch_financials(self, targets: list[dict], *, today: _dt.date | None = None,
                         force: bool = False, max_fetch: int | None = None) -> dict:
        """대상 종목의 재무제표를 `company_financial` 에 upsert.

        * 이미 있는 (종목, 연도, 보고서코드)는 건너뛴다(`force=True` 면 다시 받는다).
        * 미공시(`status='013'`)는 **오류가 아니다** — `no_data` 로 세고 넘어간다.
        """
        today = today or today_kst()
        limit = self.opts.max_fetch if max_fetch is None else max(0, int(max_fetch))
        stats = {"calls": 0, "saved": 0, "no_data": 0, "errors": 0, "skipped": 0,
                 "stocks": 0, "truncated": False}
        codes = [t["stk_cd"] for t in targets]
        if not codes:
            return stats
        mapping = {str(r["stk_cd"]): str(r["corp_code"])
                   for r in (self.db.company_corp_codes(codes) or [])}
        try:
            existing = set() if force else self.db.company_financial_keys(codes)
        except Exception:  # noqa: BLE001 - 조회 실패 시 전부 새로 받지 않고 이번 주기를 건너뛴다
            log.warning("%s: 기존 재무데이터 조회 실패 - 수집 생략", CODE, exc_info=True)
            stats["errors"] += 1
            return stats

        periods = open_periods(today, self.opts.years)
        for target in targets:
            stk_cd = target["stk_cd"]
            corp_code = mapping.get(stk_cd)
            if not corp_code:
                stats["skipped"] += 1
                continue
            have_any = any(key[0] == stk_cd for key in existing)
            wanted = periods if (force or not have_any) else periods[-RECENT_PERIODS:]
            todo = [(y, r) for (y, r) in wanted if force or (stk_cd, y, r) not in existing]
            if not todo:
                continue
            # ★ 상한 검사는 **종목 단위**로 한다. 한 종목을 중간에 끊으면 그 종목은 다음 날
            #   '데이터가 있는 종목'으로 분류돼 최근 분기만 확인하게 되고, 못 받은 과거 분기가
            #   영영 비게 된다. 예산이 모자라면 그 종목을 통째로 다음 주기로 넘긴다
            #   (단, 이번 주기에 아직 한 종목도 못 했으면 한 종목은 끝까지 한다).
            need = len(todo) * 2      # CFS 가 없으면 OFS 로 한 번 더 물어볼 수 있다
            if stats["calls"] and stats["calls"] + need > limit:
                stats["truncated"] = True
                log.info("%s: DART 호출 상한(%d)에 도달 - 남은 %d종목은 다음 주기에",
                         CODE, limit, len(targets) - stats["stocks"])
                return stats
            stats["stocks"] += 1
            shares = target.get("list_count") or None
            for year, reprt in todo:
                try:
                    values, calls = self._fetch_one(corp_code, year, reprt)
                    stats["calls"] += calls
                except DartNoData:
                    stats["calls"] += len(FS_DIVS)
                    stats["no_data"] += 1
                    continue
                except DartError as exc:
                    stats["calls"] += 1
                    stats["errors"] += 1
                    log.warning("%s: 재무제표 조회 실패 (%s %s/%s): %s",
                                CODE, stk_cd, year, REPRT_LABEL.get(reprt, reprt), exc)
                    continue
                if values is None:
                    stats["no_data"] += 1
                    continue
                values["shares_outstanding"] = shares
                try:
                    self.db.upsert_company_financial(stk_cd, year, reprt, **values)
                    stats["saved"] += 1
                except Exception:  # noqa: BLE001 - 한 건 실패가 전체를 멈추지 않게
                    stats["errors"] += 1
                    log.warning("%s: 재무제표 저장 실패 (%s %s/%s)", CODE, stk_cd, year, reprt,
                                exc_info=True)
        return stats

    def _fetch_one(self, corp_code: str, year: int, reprt: str) -> tuple[dict | None, int]:
        """연결재무제표(CFS) → 없으면 개별(OFS). `(값, 실제 호출 수)`.

        둘 다 미공시면 `DartNoData` 를 올린다(오류가 아니라 '아직 없음').
        """
        last_no_data: DartNoData | None = None
        calls = 0
        for fs_div in FS_DIVS:
            calls += 1
            try:
                rows = self.dart().single_account_all(corp_code, year, reprt, fs_div)
            except DartNoData as exc:
                last_no_data = exc
                continue
            values = parse_financial_rows(rows)
            if has_any_value(values):
                return values, calls
        if last_no_data is not None:
            raise last_no_data
        return None, calls

    # ================================================================== #
    # ③ 일별 밸류에이션
    # ================================================================== #
    def compute_valuations(self, targets: list[dict],
                           today: _dt.date | None = None) -> list[dict]:
        """최신 종가 + 최근 확정 재무제표 → `company_valuation_daily` upsert."""
        today = today or today_kst()
        codes = [t["stk_cd"] for t in targets]
        if not codes:
            return []
        try:
            closes = self.db.latest_closes(codes)
        except Exception:  # noqa: BLE001 - 일봉이 없으면 종목마스터 전일종가로 대체
            log.debug("%s: 최신 종가 조회 실패 - 종목마스터 전일종가 사용", CODE, exc_info=True)
            closes = {}
        since_year = today.year - self.opts.years + 1
        out: list[dict] = []
        for target in targets:
            stk_cd = target["stk_cd"]
            try:
                rows = self.db.company_financials(stk_cd, since_year) or []
            except Exception:  # noqa: BLE001
                log.warning("%s: 재무데이터 조회 실패 (%s)", CODE, stk_cd, exc_info=True)
                continue
            if not rows:
                continue
            price = int(closes.get(stk_cd) or target.get("last_price") or 0)
            shares = int(target.get("list_count") or 0) or _shares_from_rows(rows)
            val = compute_valuation(rows, cur_prc=price, shares=shares)
            try:
                self.db.upsert_company_valuation(
                    stk_cd, today, cur_prc=val["cur_prc"], eps_ttm=val["eps_ttm"],
                    bps=val["bps"], per=val["per"], pbr=val["pbr"], roe=val["roe"],
                    debt_ratio=val["debt_ratio"], financial_asof=val["financial_asof"])
            except Exception:  # noqa: BLE001 - 한 종목 실패가 전체를 멈추지 않게
                log.warning("%s: 밸류에이션 저장 실패 (%s)", CODE, stk_cd, exc_info=True)
                continue
            row = dict(target)
            row.update(val)
            row["financial_rows"] = rows
            out.append(row)
        return out

    # ================================================================== #
    # 온디맨드: 종목 1개만 (fundamentals_filter 전용 - services.fundamentals_fetch 가 호출)
    # ================================================================== #
    def fetch_one(self, stk_cd: str, stk_nm: str | None = None, *,
                  today: _dt.date | None = None, force: bool = False,
                  max_fetch: int | None = None) -> dict:
        """종목 1개만 corp_code 매핑 + 재무제표 수집 + 밸류에이션 계산.

        `fundamentals_filter` 가 매수 직전 "재무데이터 없음/오래됨"으로 차단한 종목을
        `FetchRequestWorker` 가 큐에서 꺼내 호출하는 경로다. 대상 종목이 `targets()`의
        시총 상위 순위에 없어도(배치 대상이 아니어도) 동작해야 하므로 `stock_master`
        를 직접 읽어 대상 1건을 만든다. **Claude 리포트는 절대 만들지 않는다**
        (`make_reports()` 를 호출하지 않는다 - 숫자만 계산하고 끝나야 비용이 0원이다).

        예외를 밖으로 던지지 않는다 - 실패 사유는 `result["errors"]` 에 모은다.
        """
        today = today or today_kst()
        result: dict = {"stk_cd": stk_cd, "stk_nm": stk_nm, "corp_code": {}, "fetch": {},
                        "valuation": None, "errors": []}
        try:
            row = self.db.stock_master_row(stk_cd)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"종목마스터 조회 실패: {type(exc).__name__}")
            log.warning("%s: 종목마스터 조회 실패 (%s)", CODE, stk_cd, exc_info=True)
            return result
        if row is None:
            result["errors"].append("종목마스터에 없는 종목")
            return result
        target = {"stk_cd": str(stk_cd), "stk_nm": row.get("stk_nm") or stk_nm,
                 "list_count": row.get("list_count"), "last_price": row.get("last_price")}

        from .corp_code_sync import CorpCodeSync

        try:
            result["corp_code"] = CorpCodeSync(self.db, self.dart()).sync(
                [stk_cd], today, force=force)
            if result["corp_code"].get("error"):
                result["errors"].append(result["corp_code"]["error"])
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"고유번호 매핑 실패: {type(exc).__name__}")
            log.exception("%s: 고유번호 매핑 실패 (%s)", CODE, stk_cd)

        try:
            mapped = bool(self.db.company_corp_codes([stk_cd]))
        except Exception:  # noqa: BLE001 - 조회 실패는 낙관적으로 두고 아래에서 알아서 skip 되게 둔다
            mapped = True
        if not mapped:
            result["errors"].append("DART 고유번호 매핑 없음(비상장/코드 불일치 가능) - 재무제표 조회 불가")
            return result

        try:
            result["fetch"] = self.fetch_financials(
                [target], today=today, force=force,
                max_fetch=self.opts.max_fetch if max_fetch is None else max_fetch)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"재무제표 수집 실패: {type(exc).__name__}")
            log.exception("%s: 재무제표 수집 실패 (%s)", CODE, stk_cd)
            return result

        try:
            vals = self.compute_valuations([target], today)
            result["valuation"] = vals[0] if vals else None
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"밸류에이션 계산 실패: {type(exc).__name__}")
            log.exception("%s: 밸류에이션 계산 실패 (%s)", CODE, stk_cd)
        return result

    # ================================================================== #
    # ④ Claude 리포트
    # ================================================================== #
    def make_reports(self, valuations: list[dict], *, today: _dt.date | None = None,
                     limit: int | None = None, force: bool = False) -> list[dict]:
        """리포트를 만들어 `company_analysis_report` 에 UPSERT(같은 날 재실행 시 갱신).

        한 종목의 실패가 나머지를 멈추지 않는다(status='error' + error_msg 로 기록).
        """
        today = today or today_kst()
        cap = self.opts.report_limit if limit is None else max(0, int(limit))
        if cap <= 0 or not valuations:
            return []
        done: set[str] = set()
        if not force:
            try:
                done = self.db.company_reported_codes(today)
            except Exception:  # noqa: BLE001 - 조회 실패는 '아직 없음'으로 본다
                log.debug("%s: 오늘자 리포트 조회 실패", CODE, exc_info=True)
        out: list[dict] = []
        for row in valuations:
            if len(out) >= cap:
                break
            if row["stk_cd"] in done:
                continue
            out.append(self._report_one(row, today))
        return out

    def _report_one(self, row: dict, today: _dt.date) -> dict:
        stk_cd, stk_nm = row["stk_cd"], row.get("stk_nm") or ""
        result = {"stk_cd": stk_cd, "stk_nm": stk_nm, "status": STATUS_OK, "summary": "",
                  "report_text": "", "model": self.opts.model, "input_tokens": 0,
                  "output_tokens": 0, "latency_ms": 0, "error_msg": ""}
        payload = build_financial_payload(
            stk_cd=stk_cd, stk_nm=stk_nm, as_of_date=today,
            rows=row.get("financial_rows") or [], valuation=row,
            market_label=row.get("market_label") or "", market_cap=row.get("market_cap"))
        try:
            res = self.claude().extract(
                build_report_prompt(payload), model=self.opts.model, system=SYSTEM_PROMPT,
                schema=FUNDAMENTAL_OUTPUT_SCHEMA, timeout_sec=self.opts.timeout_sec,
                max_tokens=REPORT_MAX_OUTPUT_TOKENS)
            parsed = validate_fundamental_output(res.data)
        except (ClaudeError, OutputSchemaError) as exc:
            result.update(status=STATUS_ERROR,
                          error_msg=f"{type(exc).__name__}: {mask_text(str(exc))[:150]}")
            log.error("%s: 리포트 생성 실패 (%s %s) - %s", CODE, stk_cd, stk_nm,
                      result["error_msg"])
        except Exception as exc:  # noqa: BLE001 - 한 종목의 예외가 전체를 죽이지 않게
            result.update(status=STATUS_ERROR,
                          error_msg=f"{type(exc).__name__}: {mask_text(str(exc))[:150]}")
            log.exception("%s: 리포트 생성 중 예외 (%s)", CODE, stk_cd)
        else:
            result.update(summary=parsed["summary"], report_text=parsed["report_text"],
                          input_tokens=res.input_tokens, output_tokens=res.output_tokens,
                          latency_ms=res.latency_ms, model=res.model or self.opts.model)
        try:
            self.db.upsert_company_report(
                stk_cd, today, model=result["model"],
                report_text=result["report_text"] or "(리포트 생성 실패)",
                stk_nm=stk_nm, summary=result["summary"] or None,
                input_tokens=result["input_tokens"] or None,
                output_tokens=result["output_tokens"] or None,
                latency_ms=result["latency_ms"] or None,
                status=result["status"], error_msg=result["error_msg"] or None)
        except Exception:  # noqa: BLE001
            log.warning("%s: 리포트 저장 실패 (%s)", CODE, stk_cd, exc_info=True)
        return result

    # ================================================================== #
    # 스케줄
    # ================================================================== #
    def due_reason(self, now: _dt.datetime | None = None) -> str:
        """지금 실행하면 안 되는 이유(빈 문자열이면 실행 가능).

        **주문 게이트·자동거래 상태를 보지 않는다** — 이 기능은 매매와 무관하다.
        """
        now = now or now_kst()
        if not self.configured:
            return "[dart] apikey_file 미설정"
        if not is_weekday(now.date()):
            return "주말(토·일) - 실행 생략"
        if last_run_date() == now.date():
            return "오늘 이미 실행 완료"
        if now.hour < self.opts.run_hour:
            return f"실행 시각({self.opts.run_hour:02d}:00) 이전"
        return ""

    def run_if_due(self, now: _dt.datetime | None = None) -> FundamentalsResult | None:
        now = now or now_kst()
        reason = self.due_reason(now)
        if reason:
            log.debug("%s 생략: %s", CODE, reason)
            return None
        _mark_run(now.date())      # 실패해도 같은 날 다시 돌지 않는다(유료 호출 폭주 방지)
        return self.run_once(now=now)

    def run_once(self, *, now: _dt.datetime | None = None, force_fetch: bool = False,
                 force_report: bool = False, report_limit: int | None = None,
                 only_codes: list[str] | None = None) -> FundamentalsResult:
        """수집 → 계산 → 리포트 1회. 예외를 밖으로 던지지 않는다."""
        now = now or now_kst()
        today = now.date()
        result = FundamentalsResult()
        try:
            targets = self.targets(now)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"대상 종목 산출 실패: {type(exc).__name__}")
            log.exception("%s: 대상 종목 산출 실패", CODE)
            return result
        if only_codes:
            keep = {str(c) for c in only_codes}
            targets = [t for t in targets if t["stk_cd"] in keep]
        result.targets = targets
        if not targets:
            result.errors.append("대상 종목 없음(종목마스터 확인 필요)")
            return result

        from .corp_code_sync import CorpCodeSync

        try:
            result.corp_code = CorpCodeSync(self.db, self.dart()).sync(
                [t["stk_cd"] for t in targets], today, force=force_fetch)
            if result.corp_code.get("error"):
                result.errors.append(result.corp_code["error"])
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"고유번호 매핑 실패: {type(exc).__name__}")
            log.exception("%s: 고유번호 매핑 실패", CODE)

        try:
            result.fetch = self.fetch_financials(targets, today=today, force=force_fetch)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"재무제표 수집 실패: {type(exc).__name__}")
            log.exception("%s: 재무제표 수집 실패", CODE)

        try:
            result.valuations = self.compute_valuations(targets, today)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"밸류에이션 계산 실패: {type(exc).__name__}")
            log.exception("%s: 밸류에이션 계산 실패", CODE)

        try:
            result.reports = self.make_reports(result.valuations, today=today,
                                               limit=report_limit, force=force_report)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"리포트 생성 실패: {type(exc).__name__}")
            log.exception("%s: 리포트 생성 실패", CODE)
        result.input_tokens = sum(r["input_tokens"] for r in result.reports)
        result.output_tokens = sum(r["output_tokens"] for r in result.reports)
        self._log_event(result)
        return result

    # ------------------------------------------------------------------ #
    def _log_event(self, result: FundamentalsResult) -> None:
        failed = sum(1 for r in result.reports if r["status"] == STATUS_ERROR)
        msg = (f"기업 재무분석(참고용, 매매 무관): 대상 {len(result.targets)}종목, "
               f"DART 호출 {result.fetch.get('calls', 0)}회(저장 {result.fetch.get('saved', 0)} / "
               f"미공시 {result.fetch.get('no_data', 0)} / 오류 {result.fetch.get('errors', 0)}), "
               f"밸류에이션 {len(result.valuations)}건, 리포트 {len(result.reports)}건"
               f"(실패 {failed}), 토큰 입력 {result.input_tokens:,}/출력 {result.output_tokens:,}")
        if result.errors:
            msg += " | " + "; ".join(result.errors[:3])
        log.info("%s", msg)
        try:
            self.db.log_event("ERROR" if result.status == STATUS_ERROR else "INFO",
                              "system", msg)
        except Exception:  # noqa: BLE001
            log.debug("재무분석 이벤트 기록 실패", exc_info=True)


def _shares_from_rows(rows) -> int:
    """종목마스터에 상장주식수가 없을 때의 대체값(수집 시점에 저장해 둔 값)."""
    for row in reversed(list(rows or [])):
        try:
            value = int(row.get("shares_outstanding") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


def describe_period(row) -> str:
    """로그·콘솔 표시용 기간 라벨."""
    return period_label(row) or "-"
