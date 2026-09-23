"""기업 재무분석(DART + Claude) 테스트.

실 DART API·실 Anthropic API·실 DB 를 쓰지 않는다 — 전부 가짜 대역이다.

핵심 검증
* corp_code 매핑은 **대상 종목만** 저장하고, 월 1회(+ 하루 1회 다운로드) 캐시가 동작한다.
* 이미 저장된 (종목, 연도, 보고서코드)는 다시 조회하지 않는다.
* 미공시 분기(status='013')는 **오류가 아니다**.
* PER/PBR/ROE/부채비율 계산이 정확하고, 분모가 0·음수·결손이면 NULL 이다.
* 리포트는 같은 날 재실행 시 갱신(UPSERT)되고, Claude 오류는 status='error' 로 격리된다.
* **매매 파이프라인(algorithm_selection·signal_log·orders·게이트)을 전혀 참조하지 않는다.**
"""
from __future__ import annotations

import ast
import datetime as _dt
import io
import zipfile
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from stock_svr.config import DartConfig
from stock_svr.dart.client import (
    FS_CONSOLIDATED,
    FS_SEPARATE,
    REPRT_ANNUAL,
    REPRT_H1,
    REPRT_Q1,
    REPRT_Q3,
    DartClient,
    DartError,
    DartNoData,
)
from stock_svr.dart.parse import corp_codes_for, parse_financial_rows, to_amount
from stock_svr.dart.valuation import compute_valuation, ttm
from stock_svr.llm.client import ClaudeError, ClaudeResult
from stock_svr.services import corp_code_sync as ccs
from stock_svr.services.corp_code_sync import CorpCodeSync
from stock_svr.services.fundamentals import (
    FundamentalsService,
    open_periods,
    period_open_date,
)

TODAY = _dt.date(2026, 9, 23)
NOW = _dt.datetime(2026, 9, 23, 17, 10)

SRC_DIR = Path(__file__).resolve().parent.parent / "stock_svr"


# ====================================================================== #
# 가짜 대역
# ====================================================================== #
def corp_xml(entries) -> str:
    rows = "".join(
        f"<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name>"
        f"<stock_code>{s}</stock_code><modify_date>{d}</modify_date></list>"
        for c, n, s, d in entries)
    return f'<?xml version="1.0" encoding="UTF-8"?><result>{rows}</result>'


class FakeDart:
    """DartClient 대역. 네트워크로 한 건도 나가지 않는다."""

    def __init__(self, xml_text: str = "", financials: dict | None = None):
        self.xml_text = xml_text
        # {(corp_code, year, reprt, fs_div): list[dict] | "no_data"}
        self.financials = financials or {}
        self.corp_calls = 0
        self.fin_calls: list[tuple] = []
        self.raise_corp: Exception | None = None

    def corp_code_xml(self) -> str:
        self.corp_calls += 1
        if self.raise_corp is not None:
            raise self.raise_corp
        return self.xml_text

    def single_account_all(self, corp_code, bsns_year, reprt_code, fs_div=FS_CONSOLIDATED):
        key = (str(corp_code), int(bsns_year), str(reprt_code), str(fs_div))
        self.fin_calls.append(key)
        value = self.financials.get(key)
        if value is None or value == "no_data":
            raise DartNoData()
        return value

    def close(self):
        pass


class FakeClaude:
    """ClaudeClient 대역(extract 만 쓴다)."""

    def __init__(self, data=None, error: Exception | None = None):
        self.data = data if data is not None else {
            "summary": "요약 문장.", "report_text": "안정성\n수익성\n성장성"}
        self.error = error
        self.calls: list[dict] = []

    def extract(self, user_text, *, model, system, schema, timeout_sec=60.0, max_tokens=1000):
        self.calls.append({"user_text": user_text, "model": model, "system": system,
                           "schema": schema})
        if self.error is not None:
            raise self.error
        res = ClaudeResult(model=model)
        res.data = self.data
        res.input_tokens, res.output_tokens, res.latency_ms = 1200, 800, 4321
        return res


def master_row(stk_cd, stk_nm, market="0", list_count=1_000_000, last_price=5_000):
    return {"stk_cd": stk_cd, "stk_nm": stk_nm, "market_code": market,
            "market_name": "코스피" if market == "0" else "코스닥",
            "list_count": list_count, "last_price": last_price,
            "state": "", "order_warning": "0"}


def fin_rows(*, revenue, op, net, assets, liab, equity, eps, ocf):
    """`fnlttSinglAcntAll.json` 응답 모양(실제 확인한 필드명 그대로)."""
    def row(sj, account_id, nm, amount):
        return {"sj_div": sj, "account_id": account_id, "account_nm": nm,
                "account_detail": "-", "thstrm_amount": str(amount),
                "thstrm_add_amount": None, "currency": "KRW"}
    return [
        row("BS", "ifrs-full_Assets", "자산총계", assets),
        row("BS", "ifrs-full_Liabilities", "부채총계", liab),
        row("BS", "ifrs-full_Equity", "자본총계", equity),
        row("IS", "ifrs-full_Revenue", "매출액", revenue),
        row("IS", "dart_OperatingIncomeLoss", "영업이익", op),
        row("IS", "ifrs-full_ProfitLoss", "당기순이익", net),
        row("IS", "ifrs-full_BasicEarningsLossPerShare", "기본주당이익(손실)", eps),
        row("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities", "영업활동현금흐름", ocf),
    ]


def make_service(db, dart=None, client=None):
    svc = FundamentalsService(db, dart=dart or FakeDart(), client=client or FakeClaude())
    return svc


def _store(db, stk_cd, row):
    """`company_financial` 한 행 저장(키 컬럼은 인자로, 값 컬럼만 kwargs 로)."""
    values = {k: v for k, v in row.items() if k not in ("bsns_year", "reprt_code")}
    db.upsert_company_financial(stk_cd, row["bsns_year"], row["reprt_code"], **values)


def seed_master(db, rows, updated_at=None):
    db.stock_master_rows = list(rows)
    db.stock_master_updated_at = updated_at or (NOW - _dt.timedelta(hours=2))


# ====================================================================== #
# 1. 파서
# ====================================================================== #
def test_to_amount_handles_commas_and_blanks():
    assert to_amount("333,605,938,000,000") == 333_605_938_000_000
    assert to_amount("-13,478,040,000,000") == -13_478_040_000_000
    assert to_amount("(1,000)") == -1000
    assert to_amount("-") is None
    assert to_amount("") is None
    assert to_amount(None) is None


def test_parse_financial_rows_uses_account_id():
    values = parse_financial_rows(fin_rows(revenue=100, op=20, net=15, assets=500,
                                           liab=200, equity=300, eps=1500, ocf=44))
    assert values["revenue"] == 100
    assert values["operating_profit"] == 20
    assert values["net_profit"] == 15
    assert values["total_assets"] == 500
    assert values["total_liabilities"] == 200
    assert values["total_equity"] == 300
    assert values["eps"] == 1500
    assert values["operating_cash_flow"] == 44


def test_parse_financial_rows_ignores_sce_breakdown():
    """자본변동표(SCE)의 `ifrs-full_Equity` 세부 행을 자본총계로 오인하지 않는다."""
    rows = [
        {"sj_div": "SCE", "account_id": "ifrs-full_Equity", "account_nm": "자본총계",
         "account_detail": "자본 [member]|자본금 [member]", "thstrm_amount": "897"},
        {"sj_div": "BS", "account_id": "ifrs-full_Equity", "account_nm": "자본총계",
         "account_detail": "-", "thstrm_amount": "436,320"},
    ]
    assert parse_financial_rows(rows)["total_equity"] == 436_320


def test_parse_financial_rows_falls_back_to_cumulative_amount():
    """3개월 칸이 비면 누적(thstrm_add_amount)으로 대체한다."""
    rows = [{"sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "매출액",
             "account_detail": "-", "thstrm_amount": "-", "thstrm_add_amount": "305,372"}]
    assert parse_financial_rows(rows)["revenue"] == 305_372


def test_parse_financial_rows_missing_fields_are_none():
    assert parse_financial_rows([])["revenue"] is None
    assert parse_financial_rows([])["eps"] is None


def test_corp_codes_for_keeps_only_targets_and_latest_modify_date():
    xml = corp_xml([
        ("00126380", "삼성전자", "005930", "20251201"),
        ("00126381", "삼성전자(구)", "005930", "20200101"),
        ("00164779", "SK하이닉스", "000660", "20250101"),
        ("00999999", "비상장회사", "", "20250101"),
        ("00888888", "관심없는회사", "111111", "20250101"),
    ])
    rows = corp_codes_for(xml, {"005930", "000660"})
    assert [r["stk_cd"] for r in rows] == ["000660", "005930"]
    assert {r["corp_code"] for r in rows} == {"00164779", "00126380"}
    # 비상장사(stock_code 공백)와 대상 밖 종목은 한 건도 포함되지 않는다
    assert all(r["stk_cd"] in ("005930", "000660") for r in rows)


# ====================================================================== #
# 2. DART 클라이언트 (httpx MockTransport - 실서버 호출 없음)
# ====================================================================== #
def make_client(handler, **kw) -> DartClient:
    cfg = DartConfig(apikey_file="", base_url="https://opendart.example", min_interval_sec=0.0)
    cfg.read_key = lambda: "0" * 40                      # type: ignore[method-assign]
    return DartClient(cfg, client_factory=lambda timeout: httpx.Client(
        transport=httpx.MockTransport(handler), timeout=1.0), **kw)


def test_client_no_data_status_is_not_an_error():
    client = make_client(lambda req: httpx.Response(
        200, json={"status": "013", "message": "조회된 데이타가 없습니다."}))
    with pytest.raises(DartNoData):
        client.single_account_all("00126380", 2026, REPRT_Q3)
    client.close()


def test_client_error_status_raises_and_never_leaks_key():
    client = make_client(lambda req: httpx.Response(
        200, json={"status": "010", "message": "등록되지 않은 키입니다."}))
    with pytest.raises(DartError) as exc:
        client.single_account_all("00126380", 2025, REPRT_ANNUAL)
    assert "010" in str(exc.value)
    assert "0" * 40 not in str(exc.value)
    client.close()


def test_client_connection_error_message_has_no_url_or_key():
    def handler(request):
        raise httpx.ConnectError("boom")

    client = make_client(handler)
    with pytest.raises(DartError) as exc:
        client.single_account_all("00126380", 2025, REPRT_ANNUAL)
    text = str(exc.value)
    assert "crtfc_key" not in text and "0" * 40 not in text
    assert exc.value.infra is True
    client.close()


def test_client_rejects_unknown_report_code():
    client = make_client(lambda req: httpx.Response(200, json={"status": "000", "list": []}))
    with pytest.raises(DartError):
        client.single_account_all("00126380", 2025, "99999")
    client.close()


def test_client_corp_code_unzips_payload():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", corp_xml([("00126380", "삼성전자", "005930", "20251201")]))
    client = make_client(lambda req: httpx.Response(200, content=buf.getvalue()))
    assert "00126380" in client.corp_code_xml()
    client.close()


def test_client_corp_code_error_xml_is_classified():
    body = b'<?xml version="1.0"?><result><status>020</status>' \
           b'<message>\xec\x9a\x94\xec\xb2\xad</message></result>'
    client = make_client(lambda req: httpx.Response(200, content=body))
    with pytest.raises(DartError) as exc:
        client.corp_code_xml()
    assert exc.value.status == "020" and exc.value.infra is True
    client.close()


# ====================================================================== #
# 3. corp_code 매핑 갱신
# ====================================================================== #
def test_corp_code_sync_saves_only_targets(fake_db):
    dart = FakeDart(corp_xml([
        ("00126380", "삼성전자", "005930", "20251201"),
        ("00164779", "SK하이닉스", "000660", "20250101"),
        ("00888888", "대상아님", "111111", "20250101"),
    ]))
    out = CorpCodeSync(fake_db, dart).sync(["005930", "000660"], TODAY)
    assert out["matched"] == 2 and out["saved"] == 2 and out["missing"] == 0
    assert set(fake_db.corp_codes) == {"005930", "000660"}
    assert fake_db.corp_codes["005930"]["corp_code"] == "00126380"


def test_corp_code_sync_counts_missing_targets(fake_db):
    dart = FakeDart(corp_xml([("00126380", "삼성전자", "005930", "20251201")]))
    out = CorpCodeSync(fake_db, dart).sync(["005930", "999999"], TODAY)
    assert out["matched"] == 1 and out["missing"] == 1


def test_corp_code_sync_skips_when_recent_and_complete(fake_db):
    dart = FakeDart(corp_xml([("00126380", "삼성전자", "005930", "20251201")]))
    sync = CorpCodeSync(fake_db, dart)
    assert sync.sync(["005930"], TODAY)["saved"] == 1
    fake_db.corp_codes_updated_at = _dt.datetime.combine(TODAY, _dt.time(9, 0))

    # 같은 날 다시 → 하루 1회 제한에 걸린다
    again = sync.sync(["005930"], TODAY)
    assert again["skipped"] and dart.corp_calls == 1

    # 3일 뒤 → 월 1회 주기가 아직 안 됐고 누락 종목도 없으므로 생략
    ccs.reset_state()
    later = sync.sync(["005930"], TODAY + _dt.timedelta(days=3))
    assert "누락 종목 없음" in later["skipped"] and dart.corp_calls == 1

    # 31일 뒤 → 주기 도달, 다시 내려받는다
    ccs.reset_state()
    fresh = sync.sync(["005930"], TODAY + _dt.timedelta(days=31))
    assert not fresh["skipped"] and dart.corp_calls == 2


def test_corp_code_sync_refreshes_when_target_missing(fake_db):
    """유니버스에 새 종목이 들어오면 주기와 무관하게 다시 받는다(하루 1회 한도 안에서)."""
    dart = FakeDart(corp_xml([
        ("00126380", "삼성전자", "005930", "20251201"),
        ("00164779", "SK하이닉스", "000660", "20250101"),
    ]))
    sync = CorpCodeSync(fake_db, dart)
    sync.sync(["005930"], TODAY)
    fake_db.corp_codes_updated_at = _dt.datetime.combine(TODAY, _dt.time(9, 0))
    ccs.reset_state()          # 날짜 캐시만 풀고 DB 상태는 그대로
    out = sync.sync(["005930", "000660"], TODAY + _dt.timedelta(days=2))
    assert not out["skipped"] and set(fake_db.corp_codes) == {"005930", "000660"}


def test_corp_code_sync_download_failure_is_not_fatal(fake_db):
    dart = FakeDart("")
    dart.raise_corp = DartError("DART 오류 status=020 (요청 제한을 초과하였습니다)", infra=True)
    out = CorpCodeSync(fake_db, dart).sync(["005930"], TODAY)
    assert out["error"] and out["saved"] == 0
    assert fake_db.corp_codes == {}


# ====================================================================== #
# 4. 공시 시점 판단
# ====================================================================== #
def test_period_open_dates_follow_disclosure_deadlines():
    assert period_open_date(2026, REPRT_Q1) == _dt.date(2026, 5, 5)
    assert period_open_date(2026, REPRT_H1) == _dt.date(2026, 8, 4)
    assert period_open_date(2026, REPRT_Q3) == _dt.date(2026, 11, 4)
    # 사업보고서는 **다음 해** 봄에 나온다
    assert period_open_date(2025, REPRT_ANNUAL) == _dt.date(2026, 3, 21)


def test_open_periods_excludes_undisclosed_quarters():
    periods = open_periods(TODAY, 5)          # 2026-09-23
    assert (2026, REPRT_H1) in periods        # 8/4 이후 → 포함
    assert (2026, REPRT_Q3) not in periods    # 11/4 이전 → 제외
    assert (2026, REPRT_ANNUAL) not in periods
    assert (2025, REPRT_ANNUAL) in periods
    assert periods[-1] == (2026, REPRT_H1)    # 최신순 정렬 확인


# ====================================================================== #
# 5. 재무제표 수집
# ====================================================================== #
def _targets(db, dart, codes=("005930",)):
    seed_master(db, [master_row(c, f"종목{c}") for c in codes])
    for c in codes:
        db.corp_codes[c] = {"stk_cd": c, "corp_code": f"00{c}", "corp_name": f"종목{c}"}
    return [{"stk_cd": c, "stk_nm": f"종목{c}", "market_code": "0", "market_label": "코스피",
             "rank": i + 1, "market_cap": 10 ** 13, "list_count": 1_000_000,
             "last_price": 5_000} for i, c in enumerate(codes)]


def test_fetch_financials_upserts_rows(fake_db):
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    dart = FakeDart(financials={("00005930", 2025, REPRT_ANNUAL, FS_CONSOLIDATED): rows})
    svc = make_service(fake_db, dart)
    targets = _targets(fake_db, dart)
    stats = svc.fetch_financials(targets, today=TODAY)
    assert stats["saved"] == 1
    saved = fake_db.financials[("005930", 2025, REPRT_ANNUAL)]
    assert saved["revenue"] == 100 and saved["eps"] == 1500
    # 상장주식수는 종목마스터(키움)에서 가져온다 - DART 추가 호출 없음
    assert saved["shares_outstanding"] == 1_000_000


def test_fetch_financials_skips_already_stored_periods(fake_db):
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    fins = {(f"00005930", y, r, FS_CONSOLIDATED): rows
            for y in range(2022, 2027) for r in (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL)}
    dart = FakeDart(financials=fins)
    svc = make_service(fake_db, dart)
    targets = _targets(fake_db, dart)

    first = svc.fetch_financials(targets, today=TODAY)
    assert first["calls"] > 4 and first["saved"] == first["calls"]
    calls_after_first = len(dart.fin_calls)

    # 두 번째 실행: 저장된 조합은 다시 조회하지 않는다
    second = svc.fetch_financials(targets, today=TODAY)
    assert second["calls"] == 0
    assert len(dart.fin_calls) == calls_after_first


def test_fetch_financials_rechecks_only_recent_periods(fake_db):
    """이미 데이터가 있는 종목은 매일 '최근 분기'만 다시 확인한다(과거 결손분 재조회 금지)."""
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    dart = FakeDart(financials={("00005930", 2025, REPRT_ANNUAL, FS_CONSOLIDATED): rows})
    svc = make_service(fake_db, dart)
    targets = _targets(fake_db, dart)
    svc.fetch_financials(targets, today=TODAY)       # 5년치 전체 시도(대부분 미공시)
    dart.fin_calls.clear()
    stats = svc.fetch_financials(targets, today=TODAY)
    # 최근 2개 기간(2025 사업보고서는 이미 저장) → 2026 1분기/반기만 다시 확인
    assert {(k[1], k[2]) for k in dart.fin_calls} == {(2026, REPRT_Q1), (2026, REPRT_H1)}
    assert stats["errors"] == 0


def test_fetch_financials_no_data_is_not_an_error(fake_db):
    dart = FakeDart(financials={})               # 전부 미공시
    svc = make_service(fake_db, dart)
    stats = svc.fetch_financials(_targets(fake_db, dart), today=TODAY)
    assert stats["no_data"] > 0 and stats["errors"] == 0
    # 기간마다 연결(CFS)·개별(OFS) 두 번 물어보고 둘 다 없으면 '미공시' 한 건으로 센다
    assert stats["calls"] == stats["no_data"] * 2
    assert fake_db.financials == {}


def test_fetch_financials_falls_back_to_separate_statements(fake_db):
    """연결재무제표가 없는 회사는 개별(OFS)로 다시 물어본다."""
    rows = fin_rows(revenue=10, op=2, net=1, assets=50, liab=20, equity=30, eps=100, ocf=4)
    dart = FakeDart(financials={("00005930", 2025, REPRT_ANNUAL, FS_SEPARATE): rows})
    svc = make_service(fake_db, dart)
    svc.fetch_financials(_targets(fake_db, dart), today=TODAY)
    assert ("005930", 2025, REPRT_ANNUAL) in fake_db.financials
    assert ("00005930", 2025, REPRT_ANNUAL, FS_CONSOLIDATED) in dart.fin_calls
    assert ("00005930", 2025, REPRT_ANNUAL, FS_SEPARATE) in dart.fin_calls


def test_fetch_financials_skips_stock_without_corp_code(fake_db):
    dart = FakeDart()
    svc = make_service(fake_db, dart)
    targets = [{"stk_cd": "005930", "stk_nm": "삼성전자", "list_count": 1, "last_price": 1}]
    stats = svc.fetch_financials(targets, today=TODAY)
    assert stats["skipped"] == 1 and stats["calls"] == 0


def _two_stock_dart():
    rows = fin_rows(revenue=1, op=1, net=1, assets=1, liab=1, equity=1, eps=1, ocf=1)
    fins = {}
    for corp in ("00005930", "00000660"):
        for year in range(2022, 2027):
            for reprt in (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL):
                fins[(corp, year, reprt, FS_CONSOLIDATED)] = rows
    return FakeDart(financials=fins)


def test_fetch_financials_defers_whole_stock_when_cap_reached(fake_db):
    """상한은 **종목 단위**로 걸린다 — 한 종목을 중간에 끊어 과거 분기를 비우지 않는다."""
    svc = make_service(fake_db, _two_stock_dart())
    targets = _targets(fake_db, svc._dart, ("005930", "000660"))
    stats = svc.fetch_financials(targets, today=TODAY, max_fetch=20)
    assert stats["truncated"] is True and stats["stocks"] == 1
    # 첫 종목은 18개 기간이 모두 저장되고, 둘째 종목은 **한 건도** 건드리지 않았다
    first = {k for k in fake_db.financials if k[0] == "005930"}
    assert len(first) == 18
    assert not any(k[0] == "000660" for k in fake_db.financials)

    # 다음 주기: 첫 종목은 최근 분기만 확인하고 둘째 종목은 5년치 전체를 받는다
    svc._dart.fin_calls.clear()
    second = svc.fetch_financials(targets, today=TODAY, max_fetch=200)
    assert second["truncated"] is False
    assert len({k for k in fake_db.financials if k[0] == "000660"}) == 18


def test_fetch_financials_allows_one_stock_to_exceed_cap(fake_db):
    """예산보다 큰 종목이라도 첫 종목은 끝까지 한다(영원히 굶지 않게)."""
    svc = make_service(fake_db, _two_stock_dart())
    stats = svc.fetch_financials(_targets(fake_db, svc._dart, ("005930",)), today=TODAY,
                                 max_fetch=3)
    assert stats["stocks"] == 1 and stats["saved"] == 18


# ====================================================================== #
# 6. 밸류에이션 계산
# ====================================================================== #
def _fin(year, reprt, **kw):
    row = {"bsns_year": year, "reprt_code": reprt, "revenue": None, "operating_profit": None,
           "net_profit": None, "total_assets": None, "total_liabilities": None,
           "total_equity": None, "eps": None, "operating_cash_flow": None,
           "shares_outstanding": None}
    row.update(kw)
    return row


def _five_quarters():
    """2025 Q1~Q3 + 2025 사업보고서(연간) + 2026 Q1."""
    return [
        _fin(2025, REPRT_Q1, eps=100, net_profit=100_000_000),
        _fin(2025, REPRT_H1, eps=200, net_profit=200_000_000),
        _fin(2025, REPRT_Q3, eps=300, net_profit=300_000_000),
        _fin(2025, REPRT_ANNUAL, eps=1000, net_profit=1_000_000_000,
             total_equity=900_000_000, total_liabilities=450_000_000),
        _fin(2026, REPRT_Q1, eps=150, net_profit=150_000_000,
             total_equity=1_000_000_000, total_liabilities=500_000_000),
    ]


def test_ttm_sums_four_consecutive_quarters():
    """4분기 단독 값은 `연간 - (Q1+Q2+Q3)` 로 만든다: 1000-600=400."""
    value, basis = ttm(_five_quarters(), "eps")
    assert value == 150 + 400 + 300 + 200      # 2026Q1 + 2025Q4 + 2025Q3 + 2025Q2
    assert "4개 분기" in basis


def test_ttm_falls_back_to_annual_report():
    rows = [_fin(2025, REPRT_ANNUAL, eps=1234)]
    value, basis = ttm(rows, "eps")
    assert value == 1234 and "사업보고서" in basis


def test_ttm_returns_none_without_data():
    value, basis = ttm([_fin(2025, REPRT_Q1)], "eps")
    assert value is None and basis


def test_compute_valuation_numbers():
    val = compute_valuation(_five_quarters(), cur_prc=5000, shares=1_000_000)
    assert val["eps_ttm"] == 1050
    assert val["bps"] == 1000                                  # 자본총계 10억 / 100만주
    assert val["per"] == Decimal("4.76")                       # 5000 / 1050
    assert val["pbr"] == Decimal("5.0000")                     # 5000 / 1000
    assert val["roe"] == Decimal("105.0000")                   # 10.5억 / 10억 × 100
    assert val["debt_ratio"] == Decimal("50.0000")             # 5억 / 10억 × 100
    assert val["financial_asof"] == "2026Q1"


def test_compute_valuation_annual_label():
    val = compute_valuation([_fin(2025, REPRT_ANNUAL, eps=1000, total_equity=100,
                                  total_liabilities=50)], cur_prc=1000, shares=10)
    assert val["financial_asof"] == "2025FY"


@pytest.mark.parametrize("equity", [0, -1_000_000])
def test_compute_valuation_null_when_equity_is_zero_or_negative(equity):
    rows = [_fin(2025, REPRT_ANNUAL, eps=1000, net_profit=100, total_equity=equity,
                 total_liabilities=500)]
    val = compute_valuation(rows, cur_prc=5000, shares=1_000)
    assert val["bps"] is None
    assert val["pbr"] is None
    assert val["roe"] is None
    assert val["debt_ratio"] is None
    # 자본과 무관한 PER 은 그대로 계산된다
    assert val["per"] == Decimal("5.00")


def test_compute_valuation_null_when_eps_not_positive():
    rows = [_fin(2025, REPRT_ANNUAL, eps=-500, net_profit=-100, total_equity=1000,
                 total_liabilities=500)]
    val = compute_valuation(rows, cur_prc=5000, shares=10)
    assert val["eps_ttm"] == -500
    assert val["per"] is None                 # 적자 → PER 없음(오류 아님)
    assert val["roe"] == Decimal("-10.0000")  # 음수 ROE 는 정상적으로 계산된다


def test_compute_valuation_null_without_price_or_shares():
    rows = [_fin(2025, REPRT_ANNUAL, eps=1000, total_equity=1000, total_liabilities=500)]
    no_price = compute_valuation(rows, cur_prc=0, shares=10)
    assert no_price["cur_prc"] is None and no_price["per"] is None and no_price["pbr"] is None
    no_shares = compute_valuation(rows, cur_prc=5000, shares=0)
    assert no_shares["bps"] is None and no_shares["pbr"] is None


def test_compute_valuation_on_empty_rows():
    val = compute_valuation([], cur_prc=5000, shares=100)
    assert val["eps_ttm"] is None and val["per"] is None and val["financial_asof"] == ""


def test_compute_valuations_writes_daily_row(fake_db):
    svc = make_service(fake_db)
    targets = _targets(fake_db, svc._dart)
    for row in _five_quarters():
        _store(fake_db, "005930", row)
    out = svc.compute_valuations(targets, TODAY)
    assert len(out) == 1
    saved = fake_db.company_valuation("005930", TODAY)
    assert saved["per"] == Decimal("4.76") and saved["financial_asof"] == "2026Q1"
    assert saved["cur_prc"] == 5000                 # stock_master.last_price


def test_compute_valuations_prefers_latest_price_daily(fake_db):
    svc = make_service(fake_db)
    targets = _targets(fake_db, svc._dart)
    fake_db.price_daily["005930"] = [{"dt": _dt.date(2026, 9, 22), "cur_prc": 7000}]
    for row in _five_quarters():
        _store(fake_db, "005930", row)
    svc.compute_valuations(targets, TODAY)
    assert fake_db.company_valuation("005930", TODAY)["cur_prc"] == 7000


def test_compute_valuations_skips_stock_without_financials(fake_db):
    svc = make_service(fake_db)
    assert svc.compute_valuations(_targets(fake_db, svc._dart), TODAY) == []
    assert fake_db.valuations == {}


# ====================================================================== #
# 7. Claude 리포트
# ====================================================================== #
def _valuation_row(db):
    for row in _five_quarters():
        _store(db, "005930", row)
    svc = make_service(db)
    return svc, svc.compute_valuations(_targets(db, svc._dart), TODAY)


def test_make_reports_saves_report(fake_db):
    svc, vals = _valuation_row(fake_db)
    reports = svc.make_reports(vals, today=TODAY, limit=1)
    assert len(reports) == 1 and reports[0]["status"] == "ok"
    saved = fake_db.company_report_on("005930", TODAY)
    assert saved["status"] == "ok" and saved["summary"] == "요약 문장."
    assert saved["input_tokens"] == 1200 and saved["output_tokens"] == 800


def test_report_prompt_contains_precomputed_numbers_only(fake_db):
    svc, vals = _valuation_row(fake_db)
    svc.make_reports(vals, today=TODAY, limit=1)
    sent = svc._client.calls[0]
    assert "\"per\": 4.76" in sent["user_text"]
    assert "\"debt_ratio_pct\": 50.0" in sent["user_text"]
    # 계좌·잔고·키 정보는 어느 것도 들어가지 않는다
    for forbidden in ("account", "계좌", "ord_alow_amt", "crtfc_key", "sk-ant"):
        assert forbidden not in sent["user_text"]
    # 웹 검색 도구를 쓰지 않는 extract 경로만 사용한다
    assert set(sent["schema"]["properties"]) == {"summary", "report_text"}


def test_make_reports_upserts_on_same_day(fake_db):
    svc, vals = _valuation_row(fake_db)
    svc.make_reports(vals, today=TODAY, limit=1)
    first = fake_db.company_report_on("005930", TODAY)

    svc._client.data = {"summary": "갱신된 요약.", "report_text": "갱신된 본문"}
    svc.make_reports(vals, today=TODAY, limit=1, force=True)
    second = fake_db.company_report_on("005930", TODAY)
    assert len(fake_db.company_reports) == 1          # 행이 늘지 않는다
    assert second["id"] == first["id"]
    assert second["summary"] == "갱신된 요약."


def test_make_reports_skips_codes_already_done_today(fake_db):
    svc, vals = _valuation_row(fake_db)
    svc.make_reports(vals, today=TODAY, limit=1)
    calls = len(svc._client.calls)
    again = svc.make_reports(vals, today=TODAY, limit=1)
    assert again == [] and len(svc._client.calls) == calls


def test_make_reports_records_claude_error(fake_db):
    for row in _five_quarters():
        _store(fake_db, "005930", row)
    svc = make_service(fake_db, client=FakeClaude(error=ClaudeError("Claude 호출 시간 초과",
                                                                   infra=True)))
    vals = svc.compute_valuations(_targets(fake_db, svc._dart), TODAY)
    reports = svc.make_reports(vals, today=TODAY, limit=1)
    assert reports[0]["status"] == "error"
    saved = fake_db.company_report_on("005930", TODAY)
    assert saved["status"] == "error" and "ClaudeError" in saved["error_msg"]


def test_make_reports_isolates_unexpected_exception(fake_db):
    for row in _five_quarters():
        _store(fake_db, "005930", row)
    svc = make_service(fake_db, client=FakeClaude(error=RuntimeError("예상 못한 오류")))
    vals = svc.compute_valuations(_targets(fake_db, svc._dart), TODAY)
    reports = svc.make_reports(vals, today=TODAY, limit=1)     # 예외가 밖으로 나오지 않는다
    assert reports[0]["status"] == "error"


def test_make_reports_rejects_invalid_schema(fake_db):
    for row in _five_quarters():
        _store(fake_db, "005930", row)
    svc = make_service(fake_db, client=FakeClaude(data={"summary": "x", "report_text": ""}))
    vals = svc.compute_valuations(_targets(fake_db, svc._dart), TODAY)
    assert svc.make_reports(vals, today=TODAY, limit=1)[0]["status"] == "error"


def test_make_reports_respects_limit(fake_db):
    codes = ("005930", "000660", "035420")
    seed_master(fake_db, [master_row(c, f"종목{c}") for c in codes])
    targets = _targets(fake_db, FakeDart(), codes)
    for code in codes:
        for row in _five_quarters():
            _store(fake_db, code, row)
    svc = make_service(fake_db)
    vals = svc.compute_valuations(targets, TODAY)
    assert len(svc.make_reports(vals, today=TODAY, limit=2)) == 2


# ====================================================================== #
# 8. 전체 실행 / 스케줄
# ====================================================================== #
def _full_service(fake_db):
    seed_master(fake_db, [master_row("005930", "삼성전자"),
                          master_row("000660", "SK하이닉스", market="10")])
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    fins = {}
    for corp in ("00126380", "00164779"):
        for year in range(2022, 2027):
            for reprt in (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL):
                fins[(corp, year, reprt, FS_CONSOLIDATED)] = rows
    dart = FakeDart(corp_xml([("00126380", "삼성전자", "005930", "20251201"),
                              ("00164779", "SK하이닉스", "000660", "20250101")]),
                    financials=fins)
    return make_service(fake_db, dart)


def test_run_once_end_to_end(fake_db):
    svc = _full_service(fake_db)
    result = svc.run_once(now=NOW, report_limit=1)
    assert len(result.targets) == 2
    assert result.corp_code["saved"] == 2
    assert result.fetch["saved"] > 0
    assert len(result.valuations) == 2
    assert len(result.reports) == 1 and result.reports[0]["status"] == "ok"
    assert result.input_tokens == 1200 and result.output_tokens == 800
    assert result.status == "ok"


def test_run_once_never_touches_trading_tables(fake_db):
    before_settings = dict(fake_db.settings)
    svc = _full_service(fake_db)
    svc.run_once(now=NOW, report_limit=1)
    # 게이트 3키를 포함해 설정이 하나도 바뀌지 않는다
    assert fake_db.settings == before_settings
    # 신호·주문·LLM 판단 로그(거부권 필터용)에 아무것도 남지 않는다
    assert fake_db.signals == [] and fake_db.orders == []
    assert fake_db.llm_decisions == []
    assert fake_db.position_states == {}


def test_run_once_is_independent_of_auto_trading_and_gate(fake_db):
    """주문 게이트가 열려 있든 닫혀 있든 결과가 같다(게이트를 아예 읽지 않는다)."""
    svc = _full_service(fake_db)
    closed = svc.run_once(now=NOW, report_limit=1)

    fake_db.financials.clear()
    fake_db.valuations.clear()
    fake_db.company_reports.clear()
    fake_db.corp_codes.clear()
    ccs.reset_state()
    fake_db.settings.update({"order_enabled": "1", "trading_mode": "real",
                             "real_trading_confirm": "1"})
    opened = svc.run_once(now=NOW, report_limit=1)

    assert len(opened.valuations) == len(closed.valuations)
    assert len(opened.reports) == len(closed.reports) == 1
    # 이 기능이 게이트를 더 열거나 닫지 않았다
    assert fake_db.settings["order_enabled"] == "1"


def test_due_reason_waits_for_run_hour(fake_db):
    svc = _full_service(fake_db)
    assert "이전" in svc.due_reason(_dt.datetime(2026, 9, 23, 10, 0))
    assert svc.due_reason(NOW) == ""


def test_run_if_due_runs_once_per_day(fake_db):
    svc = _full_service(fake_db)
    assert svc.run_if_due(NOW) is not None
    assert svc.run_if_due(NOW + _dt.timedelta(minutes=30)) is None
    assert "오늘 이미" in svc.due_reason(NOW)


def test_due_reason_requires_dart_config(fake_db):
    svc = FundamentalsService(fake_db, dart_cfg=DartConfig(apikey_file=""),
                              anthropic_cfg=None)
    assert "apikey_file" in svc.due_reason(NOW)
    assert svc.run_if_due(NOW) is None


def test_run_once_reports_target_failure(fake_db):
    """종목마스터가 비면 대상이 없다고 알리고 조용히 끝난다(예외 없음)."""
    svc = _full_service(fake_db)
    fake_db.stock_master_rows = []
    result = svc.run_once(now=NOW)
    assert result.targets == [] and result.errors


def test_targets_exclude_etf_and_preferred(fake_db):
    seed_master(fake_db, [
        master_row("005930", "삼성전자", market="0", list_count=1000, last_price=1000),
        master_row("005935", "삼성전자우", market="0", list_count=1000, last_price=900),
        master_row("069500", "KODEX 200", market="8", list_count=1000, last_price=800),
        master_row("000660", "SK하이닉스", market="10", list_count=500, last_price=700),
    ])
    svc = make_service(fake_db)
    codes = {t["stk_cd"] for t in svc.targets(NOW)}
    assert codes == {"005930", "000660"}      # ETF(8)·우선주 제외


# ====================================================================== #
# 9. 매매 파이프라인과의 분리 (소스 검사)
# ====================================================================== #
FUNDAMENTAL_MODULES = (
    SRC_DIR / "dart" / "client.py",
    SRC_DIR / "dart" / "parse.py",
    SRC_DIR / "dart" / "valuation.py",
    SRC_DIR / "services" / "fundamentals.py",
    SRC_DIR / "services" / "corp_code_sync.py",
    SRC_DIR / "llm" / "fundamental_prompt.py",
)

# 코드(주석·docstring 제외)에 나타나면 안 되는 매매 파이프라인 식별자
FORBIDDEN_SYMBOLS = (
    "algorithm_selection", "algorithm_param_value", "algorithm_param_def",
    "load_algorithms", "save_selection", "save_param",
    "signal_log", "insert_signal", "Signal", "signal_id",
    "insert_order", "upsert_order_by_ordno", "orders_unlocked", "unlock_orders",
    "order_enabled", "real_trading_confirm", "can_send_order", "set_gate",
    "GATE_CONFIRM_TOKEN", "OrderGateState", "Executor", "risk_guard",
    "KiwoomRest", "auto_trading",
)


def _code_symbols(path: Path) -> list[str]:
    """주석·docstring 을 뺀 **실제 코드**의 식별자/문자열만 모은다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if ast.get_docstring(node, clean=False) is not None:
                doc_nodes.add(id(node.body[0].value))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.Name):
            out.append(node.id)
        elif isinstance(node, ast.arg):
            out.append(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            out.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node.name)
        elif isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
            out += [a.name for a in node.names] + [a.asname or "" for a in node.names]
        elif isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in doc_nodes:
                out.append(node.value)
    return out


@pytest.mark.parametrize("path", FUNDAMENTAL_MODULES, ids=lambda p: p.name)
def test_fundamentals_modules_do_not_reference_trading_pipeline(path):
    symbols = _code_symbols(path)
    hits = sorted({s for s in FORBIDDEN_SYMBOLS
                   for sym in symbols if s.lower() in sym.lower()})
    assert hits == [], f"{path.name} 이 매매 파이프라인을 참조합니다: {hits}"


def test_fundamentals_is_not_registered_as_an_algorithm():
    """`algorithm` 레지스트리에 등록되지 않는다(매매 사이클에서 평가되지 않는다)."""
    from stock_svr.algo import registry
    from stock_svr.services.fundamentals import CODE

    assert CODE not in registry.known_codes()
    assert registry.get(CODE) is None
    # 이 기능이 알고리즘 인스턴스를 만들 수 있었다면 평가 사이클에 끼어들 수 있다
    assert registry.build({"code": CODE}) is None


def test_engine_polls_fundamentals_outside_auto_trading_branch():
    """runner 의 폴링이 자동거래 스위치와 **별도 상수·별도 분기**로 돌아간다."""
    from stock_svr.engine import runner
    from stock_svr.services.fundamentals import POLL_SEC

    source = (SRC_DIR / "engine" / "runner.py").read_text(encoding="utf-8")
    start = source.index("last_fundamentals = now_mono")
    block = source[source.rindex("if ", 0, start):start]
    assert "auto_trading" not in block and "gate" not in block
    assert runner.FUNDAMENTALS_POLL_SEC == POLL_SEC
    assert POLL_SEC != runner.TREND_REQUEST_POLL_SEC
