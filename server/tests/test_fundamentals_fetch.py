"""온디맨드 재무데이터 수집 (fundamentals_filter 전용) 테스트.

실 DART API·실 DB 를 쓰지 않는다 — 전부 가짜 대역(FakeDb/FakeDart, test_fundamentals 의
대역을 재사용한다 - test_audit_fixes.py 가 test_auto_trading.py 를 재사용하는 것과 동일한
이 저장소의 기존 관례).

핵심 검증
* `enqueue()` dedupe: 이미 pending/processing 이거나 최근(쿨다운 이내) 처리 이력이 있으면
  다시 넣지 않는다. 예외는 삼키고 호출부에 절대 전파하지 않는다.
* `FetchRequestWorker`: claim 은 원자적(경쟁에서 진 쪽은 처리하지 않음)이고, 종목 1개만
  corp_code 매핑 + 재무제표 수집 + 밸류에이션 계산을 하며 **Claude 는 절대 호출하지 않는다**.
* 매매 파이프라인(게이트·algorithm_selection·signal_log·orders·risk_guard·Executor)을
  전혀 참조하지 않는다(AST 로 강제).
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from conftest import FakeDb
from stock_svr.dart.client import REPRT_ANNUAL, REPRT_H1, REPRT_Q1, REPRT_Q3, DartError, DartNoData
from stock_svr.services import fundamentals_fetch as ff
from stock_svr.util import today_kst
from test_fundamentals import (
    FORBIDDEN_SYMBOLS,
    SRC_DIR,
    FakeClaude,
    FakeDart,
    _code_symbols,
    corp_xml,
    fin_rows,
    make_service,
    master_row,
    seed_master,
)

NOW = _dt.datetime(2026, 9, 23, 10, 30)
TODAY = NOW.date()


def request_db(*, now=None) -> FakeDb:
    db = FakeDb()
    db.now_for_stale = now or NOW
    return db


# ====================================================================== #
# 1. enqueue() — dedupe + 예외 격리
# ====================================================================== #
def test_enqueue_inserts_new_request():
    db = request_db()
    ok = ff.enqueue(db, "005930", "삼성전자")
    assert ok is True
    assert len(db.fetch_requests) == 1
    req = db.fetch_requests[0]
    assert req["stk_cd"] == "005930" and req["stk_nm"] == "삼성전자"
    assert req["status"] == "pending" and req["source"] == "fundamentals_filter"


def test_enqueue_skips_when_pending_exists():
    db = request_db()
    db.insert_fetch_request("005930")
    assert ff.enqueue(db, "005930") is False
    assert len(db.fetch_requests) == 1


def test_enqueue_skips_when_processing_exists():
    db = request_db()
    db.insert_fetch_request("005930")
    db.fetch_requests[0]["status"] = "processing"
    assert ff.enqueue(db, "005930") is False
    assert len(db.fetch_requests) == 1


def test_enqueue_skips_when_recently_completed_within_cooldown():
    db = request_db()
    db.insert_fetch_request("005930")
    db.fetch_requests[0].update(
        status="done", finished_at=NOW - _dt.timedelta(minutes=30))
    assert ff.enqueue(db, "005930") is False
    assert len(db.fetch_requests) == 1


def test_enqueue_allows_when_completed_outside_cooldown():
    db = request_db()
    db.insert_fetch_request("005930")
    db.fetch_requests[0].update(
        status="done", finished_at=NOW - _dt.timedelta(minutes=90))
    assert ff.enqueue(db, "005930") is True
    assert len(db.fetch_requests) == 2


def test_enqueue_allows_different_stock_even_if_one_is_open():
    db = request_db()
    db.insert_fetch_request("005930")
    assert ff.enqueue(db, "000660") is True
    assert len(db.fetch_requests) == 2


def test_enqueue_swallows_db_exception_on_dedupe_check():
    db = request_db()
    db.fail_on.add("open_fetch_request")
    assert ff.enqueue(db, "005930") is False
    assert db.fetch_requests == []


def test_enqueue_swallows_db_exception_on_insert():
    db = request_db()
    db.fail_on.add("insert_fetch_request")
    assert ff.enqueue(db, "005930") is False


def test_enqueue_empty_code_returns_false_without_db_access():
    db = request_db()
    db.fail_on.add("open_fetch_request")     # 호출되면 예외 → 아예 조회하지 않음을 검증
    assert ff.enqueue(db, "") is False
    assert ff.enqueue(db, None) is False
    assert db.fetch_requests == []


# ====================================================================== #
# 2. claim — 원자적 경쟁 해결 (trend_scan_request 와 동일한 방식)
# ====================================================================== #
def test_pending_request_is_claimed_atomically():
    db = request_db()
    row = db.insert_fetch_request("005930")
    first = db.claim_fetch_request()
    second = db.claim_fetch_request()
    assert first and first["id"] == row and first["status"] == "processing"
    assert second is None


def test_claim_race_loser_does_not_process():
    """조회와 UPDATE 사이에 남이 먼저 집으면 영향 행 수가 0 이라 포기한다."""
    db = request_db()
    db.insert_fetch_request("005930")
    stale = dict(db.fetch_requests[0])
    assert db.claim_fetch_request() is not None
    db.pending_fetch_request = lambda: dict(stale)
    assert db.claim_fetch_request() is None


def test_only_one_in_flight_request_processed_at_a_time():
    db = request_db()
    db.insert_fetch_request("005930")
    db.fetch_requests[0]["status"] = "processing"
    db.insert_fetch_request("000660")
    svc = make_service(db)
    worker = ff.FetchRequestWorker(db, svc)
    assert worker.poll_once() is None
    assert db.fetch_requests[1]["status"] == "pending"


def test_poll_once_returns_none_when_no_requests():
    db = request_db()
    svc = make_service(db)
    assert ff.FetchRequestWorker(db, svc).poll_once() is None


# ====================================================================== #
# 3. stale(처리 중 멈춤) 정리
# ====================================================================== #
def test_cleanup_stale_expires_long_processing_requests():
    db = request_db(now=NOW)
    db.insert_fetch_request("005930")
    db.fetch_requests[0].update(status="processing", requested_at=NOW - _dt.timedelta(minutes=20))
    svc = make_service(db)
    n = ff.FetchRequestWorker(db, svc).cleanup_stale(minutes=10)
    assert n == 1
    assert db.fetch_requests[0]["status"] == "error"
    assert any("정리" in e[2] for e in db.events)


def test_cleanup_stale_ignores_recent_processing():
    db = request_db(now=NOW)
    db.insert_fetch_request("005930")
    db.fetch_requests[0].update(status="processing", requested_at=NOW - _dt.timedelta(minutes=2))
    svc = make_service(db)
    n = ff.FetchRequestWorker(db, svc).cleanup_stale(minutes=10)
    assert n == 0
    assert db.fetch_requests[0]["status"] == "processing"


# ====================================================================== #
# 4. 종목 1개 수집 (FundamentalsService.fetch_one 경유) — 성공/실패/미공시
# ====================================================================== #
def _service_for_one_stock(db, *, corp_ok=True, financials=None):
    seed_master(db, [master_row("005930", "삼성전자")])
    xml = corp_xml([("00126380", "삼성전자", "005930", "20251201")]) if corp_ok else corp_xml([])
    dart = FakeDart(xml, financials=financials or {})
    claude = FakeClaude()
    return make_service(db, dart, claude), claude


def test_worker_processes_request_successfully():
    db = request_db(now=NOW)
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    fins = {("00126380", y, r, "CFS"): rows
           for y in range(2022, 2027) for r in (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL)}
    svc, claude = _service_for_one_stock(db, financials=fins)
    db.insert_fetch_request("005930", "삼성전자")

    out = ff.FetchRequestWorker(db, svc).poll_once()

    assert out["status"] == "done"
    assert db.fetch_requests[0]["status"] == "done"
    assert db.fetch_requests[0]["error_msg"] is None
    # FetchRequestWorker.poll_once() 는 `now`/`today` 를 주입받지 않고 그대로
    # FundamentalsService.fetch_one() 의 기본값(today_kst(), 실제 오늘 날짜)을 쓴다 —
    # request_db(now=NOW) 의 NOW(stale 판정용 고정 시각)와는 별개다. 그래서 여기서는
    # 고정된 TODAY 가 아니라 실제 오늘 날짜로 검증한다(테스트를 실행하는 날과 무관하게
    # 항상 맞아야 함 - 날짜가 바뀌면서 TODAY 하드코딩이 깨졌던 문제를 고친 것).
    assert ("005930", today_kst()) in db.valuations
    assert claude.calls == []          # Claude 는 절대 호출되지 않는다


def test_worker_records_no_disclosure_as_done_not_error():
    """DART 가 전부 미공시(status='013')를 주면 오류가 아니라 done 으로 끝난다."""
    db = request_db(now=NOW)
    svc, claude = _service_for_one_stock(db, financials={})   # 전부 no_data
    db.insert_fetch_request("005930", "삼성전자")

    out = ff.FetchRequestWorker(db, svc).poll_once()

    assert out["status"] == "done"
    assert ("005930", TODAY) not in db.valuations       # 재무데이터가 없어 계산도 없음
    assert claude.calls == []


def test_worker_marks_error_when_corp_code_mapping_missing():
    db = request_db(now=NOW)
    svc, claude = _service_for_one_stock(db, corp_ok=False)   # corpCode.xml 에 매칭 없음
    db.insert_fetch_request("005930", "삼성전자")

    out = ff.FetchRequestWorker(db, svc).poll_once()

    assert out["status"] == "error"
    assert "매핑" in db.fetch_requests[0]["error_msg"]
    assert claude.calls == []


def test_worker_marks_error_when_stock_not_in_master():
    db = request_db(now=NOW)
    svc, claude = _service_for_one_stock(db)
    db.stock_master_rows = []          # 종목마스터에 없는 종목
    db.insert_fetch_request("999999", "없는종목")

    out = ff.FetchRequestWorker(db, svc).poll_once()

    assert out["status"] == "error"
    assert db.fetch_requests[0]["error_msg"]


def test_worker_marks_error_when_dart_not_configured():
    db = request_db(now=NOW)
    seed_master(db, [master_row("005930", "삼성전자")])
    from stock_svr.services.fundamentals import FundamentalsService

    svc = FundamentalsService(db)   # dart_cfg/dart 둘 다 없음
    db.insert_fetch_request("005930", "삼성전자")

    out = ff.FetchRequestWorker(db, svc).poll_once()
    assert out["status"] == "error"


def test_worker_catches_unexpected_exception_from_fetch_one():
    db = request_db(now=NOW)
    svc, _ = _service_for_one_stock(db)

    def boom(*a, **kw):
        raise RuntimeError("예상치 못한 오류(테스트)")

    svc.fetch_one = boom
    db.insert_fetch_request("005930", "삼성전자")

    out = ff.FetchRequestWorker(db, svc).poll_once()
    assert out["status"] == "error"
    assert db.fetch_requests[0]["status"] == "error"


# ====================================================================== #
# 5. 매매 파이프라인과의 분리
# ====================================================================== #
def test_worker_never_touches_trading_tables():
    db = request_db(now=NOW)
    before_settings = dict(db.settings)
    rows = fin_rows(revenue=100, op=20, net=15, assets=500, liab=200, equity=300,
                    eps=1500, ocf=44)
    fins = {("00126380", y, r, "CFS"): rows
           for y in range(2022, 2027) for r in (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL)}
    svc, claude = _service_for_one_stock(db, financials=fins)
    db.insert_fetch_request("005930", "삼성전자")

    ff.FetchRequestWorker(db, svc).poll_once()

    assert db.settings == before_settings
    assert db.signals == [] and db.orders == []
    assert db.llm_decisions == [] and db.company_reports == {}
    assert claude.calls == []


def test_fundamentals_fetch_module_does_not_reference_trading_pipeline():
    path = SRC_DIR / "services" / "fundamentals_fetch.py"
    symbols = _code_symbols(path)
    hits = sorted({s for s in FORBIDDEN_SYMBOLS for sym in symbols if s.lower() in sym.lower()})
    assert hits == [], f"fundamentals_fetch.py 가 매매 파이프라인을 참조합니다: {hits}"


def test_fundamentals_fetch_module_never_calls_claude_or_make_reports():
    path = SRC_DIR / "services" / "fundamentals_fetch.py"
    symbols = _code_symbols(path)
    assert "make_reports" not in symbols
    assert not any("claude" in s.lower() for s in symbols), \
        "fundamentals_fetch.py 가 실제 코드에서 Claude 를 참조하면 안 된다(주석/문서만 허용)"


def test_fundamentals_fetch_is_not_registered_as_an_algorithm():
    from stock_svr.algo import registry

    assert ff.CODE not in registry.known_codes()
    assert registry.get(ff.CODE) is None
    assert registry.build({"code": ff.CODE}) is None


def test_engine_polls_fetch_requests_outside_auto_trading_branch():
    """runner 의 폴링이 자동거래 스위치와 **별도 상수·별도 분기**로 돌아간다."""
    from stock_svr.engine import runner

    source = (SRC_DIR / "engine" / "runner.py").read_text(encoding="utf-8")
    start = source.index("last_fetch_req = now_mono")
    block = source[source.rindex("if ", 0, start):start]
    assert "auto_trading" not in block and "gate" not in block
    assert runner.FETCH_REQUEST_POLL_SEC == ff.FETCH_REQUEST_POLL_SEC
    assert runner.FETCH_REQUEST_POLL_SEC != runner.TREND_REQUEST_POLL_SEC


def test_engine_builds_fetch_request_worker_in_startup():
    source = (SRC_DIR / "engine" / "runner.py").read_text(encoding="utf-8")
    assert "FetchRequestWorker(self.db, self.fundamentals)" in source
