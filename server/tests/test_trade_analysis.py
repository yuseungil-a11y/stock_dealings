"""거래 종합분석 리포트(Claude, 참고용) 테스트.

실 Anthropic API·실 DB 를 쓰지 않는다 - FakeDb + FakeClaude 대역.

검증 범위
  · daily_trade_summary → 승/패/손익/승률 집계가 정확하다
  · v_trade_analysis 로 개별 거래의 맥락(알고리즘·사유·슬리피지·Claude 사전 의견)이 보강된다
  · 거래가 많은 기간은 손익 절대값 상위 N건만 상세로, 나머지는 집계로 캡한다
  · 조회 기간에 오늘이 포함되면 "당일 데이터가 불완전할 수 있다"는 안내가 붙는다
  · Claude 오류/스키마 위반 → status='error' (엔진을 죽이지 않는다)
  · 요청 큐는 trend_scan_request 와 동일한 claim/stale 정리 패턴을 따른다
  · 매매 신호·주문·게이트는 전혀 건드리지 않는다(읽기만 한다)
"""
from __future__ import annotations

import datetime as _dt

from conftest import FakeDb
from stock_svr.llm.client import ClaudeError, ClaudeResult
from stock_svr.llm.trade_analysis_prompt import MAX_DETAIL_TRADES, build_trade_payload
from stock_svr.services.trade_analysis import (
    STATUS_ERROR,
    STATUS_OK,
    TradeAnalysisService,
    TradeAnalysisWorker,
)

ACCOUNT_ID = 1
START = _dt.date(2026, 9, 22)
END = _dt.date(2026, 9, 29)


# ====================================================================== #
# 가짜 Claude 클라이언트 (extract 만 쓴다 - fundamentals/trend_scan 과 같은 패턴)
# ====================================================================== #
class FakeClaude:
    """ClaudeClient 대역(extract 만 쓴다)."""

    def __init__(self, data=None, error: Exception | None = None):
        self.data = data if data is not None else {
            "summary": "요약 문장.", "report_text": "요약\n개별 거래\n공통 패턴\n참고 제안"}
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


# ====================================================================== #
# 헬퍼
# ====================================================================== #
def summary_row(stk_cd, base_dt, pl_amt, *, stk_nm="테스트종목", buy_qty=10, sell_qty=10,
                buy_avg=1000, sell_avg=1100, prft_rt=None):
    return {"account_id": ACCOUNT_ID, "base_dt": base_dt, "stk_cd": stk_cd, "stk_nm": stk_nm,
            "buy_qty": buy_qty, "buy_avg_pric": buy_avg, "buy_amt": buy_qty * buy_avg,
            "sell_qty": sell_qty, "sell_avg_pric": sell_avg, "sell_amt": sell_qty * sell_avg,
            "cmsn_tax": 100, "pl_amt": pl_amt, "prft_rt": prft_rt}


def ctx_row(stk_cd, when, *, algo_code="macd_cross", detail="MACD 골든크로스",
           llm_decision="allow", slippage=0.5):
    return {"stk_cd": stk_cd, "signal_time": when, "order_time": when, "algo_code": algo_code,
            "signal_detail": detail, "order_reason": None, "llm_decision": llm_decision,
            "llm_reasons": "근거", "slippage_pct": slippage}


def service(db, client=None) -> TradeAnalysisService:
    return TradeAnalysisService(db, client=client or FakeClaude())


def worker(db, client=None) -> TradeAnalysisWorker:
    return TradeAnalysisWorker(db, account_id=ACCOUNT_ID, client=client or FakeClaude())


# ====================================================================== #
# 1. 집계 / 맥락 보강 / payload 캡
# ====================================================================== #
def test_거래가_없으면_NoTradesError_로_error_처리된다():
    db = FakeDb()
    result = service(db).run(ACCOUNT_ID, START, END)
    assert result.status == STATUS_ERROR
    assert not result.attempted           # Claude 호출 자체를 안 함
    assert "매매 기록이 없습니다" in result.error_msg


def test_승패_집계가_정확하다():
    db = FakeDb()
    db.daily_trade_summaries = [
        summary_row("000001", START, 10000),
        summary_row("000002", START, -5000),
        summary_row("000003", START, 0),
    ]
    trades, agg = service(db).build_trades(ACCOUNT_ID, START, END)
    assert agg["trade_count"] == 3
    assert agg["win_count"] == 1
    assert agg["loss_count"] == 1
    assert agg["total_pl_amt"] == 5000
    assert agg["win_rate"] == 50.0
    assert len(trades) == 3


def test_v_trade_analysis_로_맥락이_보강된다():
    db = FakeDb()
    when = _dt.datetime(2026, 9, 22, 9, 30)
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.trade_analysis_v_rows = [ctx_row("000001", when)]
    trades, _ = service(db).build_trades(ACCOUNT_ID, START, END)
    assert trades[0]["entry_algo"] == "macd_cross"
    assert trades[0]["entry_reason"] == "MACD 골든크로스"
    assert trades[0]["claude_reviewed"] is True
    assert trades[0]["claude_decision"] == "allow"
    assert trades[0]["slippage_pct"] == 0.5


def test_기간_밖의_맥락은_보강되지_않는다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.trade_analysis_v_rows = [ctx_row("000001", _dt.datetime(2026, 9, 1, 9, 30))]
    trades, _ = service(db).build_trades(ACCOUNT_ID, START, END)
    assert trades[0]["entry_algo"] is None


def test_거래가_많으면_상위N건만_상세로_보내고_나머지는_집계한다():
    n = MAX_DETAIL_TRADES + 10
    trades = [{"stk_cd": f"{i:06d}", "pl_amt": (i + 1) * 1000 * (1 if i % 2 == 0 else -1)}
              for i in range(n)]
    payload = build_trade_payload(period_start=START, period_end=END, trades=trades,
                                  aggregate={"trade_count": n})
    assert len(payload["trades"]) == MAX_DETAIL_TRADES
    assert payload["other_trades_aggregate"]["trade_count"] == 10
    # 캡 대상은 손익 절대값이 가장 큰 거래들이어야 한다
    biggest = max(abs(t["pl_amt"]) for t in trades)
    assert any(abs(t["pl_amt"]) == biggest for t in payload["trades"])


def test_거래가_상한_이하이면_전부_상세로_들어가고_집계블록이_없다():
    trades = [{"stk_cd": "000001", "pl_amt": 100}]
    payload = build_trade_payload(period_start=START, period_end=END, trades=trades,
                                  aggregate={"trade_count": 1})
    assert len(payload["trades"]) == 1
    assert "other_trades_aggregate" not in payload


def test_오늘이_기간에_포함되면_불완전_안내가_붙는다():
    payload = build_trade_payload(period_start=START, period_end=END,
                                  trades=[{"stk_cd": "000001", "pl_amt": 100}],
                                  aggregate={"trade_count": 1}, today_incomplete=True)
    assert "today_caveat" in payload["notes"]


def test_오늘이_기간에_없으면_불완전_안내가_없다():
    payload = build_trade_payload(period_start=START, period_end=END,
                                  trades=[{"stk_cd": "000001", "pl_amt": 100}],
                                  aggregate={"trade_count": 1}, today_incomplete=False)
    assert "today_caveat" not in payload["notes"]


# ====================================================================== #
# 2. Claude 호출 성공/실패 (service.run)
# ====================================================================== #
def test_정상_분석은_report_text와_summary를_돌려준다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    client = FakeClaude()
    result = service(db, client).run(ACCOUNT_ID, START, END, requested_by="admin")
    assert result.status == STATUS_OK
    assert result.summary and result.report_text
    assert result.attempted
    assert len(client.calls) == 1


def test_Claude_오류는_status만_error로_바뀌고_집계는_유지된다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    client = FakeClaude(error=ClaudeError("네트워크 실패", infra=True))
    result = service(db, client).run(ACCOUNT_ID, START, END)
    assert result.status == STATUS_ERROR
    assert result.attempted                  # 이미 집계는 계산됐으므로 리포트 행은 만든다
    assert result.trade_count == 1
    assert "네트워크 실패" in result.error_msg


def test_스키마_위반_응답은_error로_처리된다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    client = FakeClaude(data={"summary": "요약"})   # report_text 없음 → 스키마 위반
    result = service(db, client).run(ACCOUNT_ID, START, END)
    assert result.status == STATUS_ERROR
    assert "스키마" in result.error_msg


# ====================================================================== #
# 3. 요청 큐 (claim / 경합 / stale 정리)
# ====================================================================== #
def test_pending_요청을_원자적으로_claim_한다():
    db = FakeDb()
    row = db.add_trade_analysis_request()
    first = db.claim_trade_analysis_request()
    second = db.claim_trade_analysis_request()
    assert first and first["id"] == row["id"] and first["status"] == "processing"
    assert second is None                        # 같은 건을 두 번 집지 않는다


def test_claim_경쟁에서_진_쪽은_처리하지_않는다():
    db = FakeDb()
    db.add_trade_analysis_request()
    stale = dict(db.trade_analysis_requests[0])
    assert db.claim_trade_analysis_request() is not None
    db.pending_trade_analysis_request = lambda: dict(stale)
    assert db.claim_trade_analysis_request() is None


def test_이미_처리중인_요청이_있으면_새_요청을_집지_않는다():
    """동시에 1건만 처리한다(유료 호출 낭비 방지)."""
    db = FakeDb()
    db.add_trade_analysis_request()
    db.trade_analysis_requests[0]["status"] = "processing"
    db.add_trade_analysis_request()
    client = FakeClaude()
    assert worker(db, client).poll_once() is None
    assert client.calls == []
    assert db.trade_analysis_requests[1]["status"] == "pending"


def test_요청이_없으면_아무것도_하지_않는다():
    db = FakeDb()
    client = FakeClaude()
    assert worker(db, client).poll_once() is None
    assert client.calls == []


def test_오래_멈춘_processing_요청은_error_로_정리된다():
    db = FakeDb()
    db.now_for_stale = _dt.datetime(2026, 9, 29, 9, 0)
    db.add_trade_analysis_request(requested_at=_dt.datetime(2026, 9, 29, 8, 40))  # 20분 전
    db.trade_analysis_requests[0]["status"] = "processing"
    assert worker(db).cleanup_stale(15) == 1
    assert db.trade_analysis_requests[0]["status"] == "error"
    assert "서버 재시작" in db.trade_analysis_requests[0]["error_msg"]


def test_최근_processing_요청은_정리하지_않는다():
    db = FakeDb()
    db.now_for_stale = _dt.datetime(2026, 9, 29, 8, 45)
    db.add_trade_analysis_request(requested_at=_dt.datetime(2026, 9, 29, 8, 40))  # 5분 전
    db.trade_analysis_requests[0]["status"] = "processing"
    assert worker(db).cleanup_stale(15) == 0
    assert db.trade_analysis_requests[0]["status"] == "processing"


def test_멈춘_요청을_정리하면_다음_요청이_처리된다():
    db = FakeDb()
    db.now_for_stale = _dt.datetime(2026, 9, 29, 9, 0)
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.add_trade_analysis_request(period_start=START, period_end=END,
                                  requested_at=_dt.datetime(2026, 9, 29, 8, 30))
    db.trade_analysis_requests[0]["status"] = "processing"       # 서버가 죽으며 남은 행
    db.add_trade_analysis_request(period_start=START, period_end=END,
                                  requested_at=_dt.datetime(2026, 9, 29, 8, 55))
    out = worker(db).poll_once()
    assert out and out["status"] == STATUS_OK
    assert db.trade_analysis_requests[0]["status"] == "error"
    assert db.trade_analysis_requests[1]["status"] == "done"


# ====================================================================== #
# 4. 요청 처리 (성공/실패 전체 경로 - report 행 작성 포함)
# ====================================================================== #
def test_처리에_성공하면_done과_report_id와_report_행이_기록된다():
    db = FakeDb()
    db.daily_trade_summaries = [
        summary_row("000001", START, 10000),
        summary_row("000002", START, -3000),
    ]
    db.add_trade_analysis_request(period_start=START, period_end=END, requested_by="admin")
    out = worker(db).poll_once()
    assert out["status"] == STATUS_OK
    req = db.trade_analysis_requests[0]
    assert req["status"] == "done"
    assert req["report_id"] == out["report_id"]
    assert not req["error_msg"]

    report = db.trade_analysis_reports[0]
    assert report["id"] == out["report_id"]
    assert report["account_id"] == ACCOUNT_ID
    assert report["period_start"] == START and report["period_end"] == END
    assert report["requested_by"] == "admin"
    assert report["trade_count"] == 2
    assert report["win_count"] == 1 and report["loss_count"] == 1
    assert report["total_pl_amt"] == 7000
    assert report["status"] == "ok"
    assert report["summary"] and report["report_text"]
    assert report["input_tokens"] == 1200 and report["output_tokens"] == 800


def test_거래기록이_없으면_리포트를_만들지_않고_요청만_error가_된다():
    """비용 낭비 방지 - 거래가 없으면 Claude 를 호출하지 않는다."""
    db = FakeDb()
    client = FakeClaude()
    db.add_trade_analysis_request(period_start=START, period_end=END)
    out = worker(db, client).poll_once()
    assert out["status"] == STATUS_ERROR
    assert client.calls == []                    # Claude 호출 0
    assert db.trade_analysis_reports == []        # 리포트 행도 만들지 않는다
    req = db.trade_analysis_requests[0]
    assert req["status"] == "error"
    assert req["report_id"] is None
    assert "매매 기록이 없습니다" in req["error_msg"]


def test_Claude_오류여도_요청_처리는_죽지_않고_error_리포트가_남는다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.add_trade_analysis_request(period_start=START, period_end=END)
    client = FakeClaude(error=ClaudeError("타임아웃", infra=True))
    out = worker(db, client).poll_once()
    assert out["status"] == STATUS_ERROR
    req = db.trade_analysis_requests[0]
    assert req["status"] == "error" and req["error_msg"]

    report = db.trade_analysis_reports[0]
    assert report["status"] == "error"
    assert report["trade_count"] == 1             # 집계는 Claude 호출 전에 이미 계산돼 있었다
    assert report["report_text"] == "(리포트 생성 실패)"


def test_처리_중_예외가_나도_폴링_루프는_죽지_않는다():
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.add_trade_analysis_request(period_start=START, period_end=END)
    db.fail_on.add("insert_trade_analysis_report")     # 리포트 저장 자체가 터진다
    out = worker(db).poll_once()
    assert out["status"] == STATUS_ERROR and out["error"]
    assert db.trade_analysis_requests[0]["status"] == "error"
    # 다음 주기도 정상 동작한다
    db.fail_on.clear()
    db.add_trade_analysis_request(period_start=START, period_end=END)
    assert worker(db).poll_once()["status"] == STATUS_OK


def test_DB_조회가_실패해도_폴링은_조용히_넘어간다():
    db = FakeDb()
    db.fail_on.add("count_processing_trade_analysis_requests")
    assert worker(db).poll_once() is None


def test_요청_처리는_매매_설정을_전혀_바꾸지_않는다():
    """읽기만 한다 - signal_log/orders/게이트를 건드리지 않는다."""
    db = FakeDb()
    db.daily_trade_summaries = [summary_row("000001", START, 10000)]
    db.add_trade_analysis_request(period_start=START, period_end=END)
    before_settings = dict(db.settings)
    worker(db).poll_once()
    assert db.settings == before_settings
    assert db.signals == [] and db.orders == []


# ====================================================================== #
# 5. 엔진 배선
# ====================================================================== #
def test_폴링_주기는_하트비트처럼_짧다():
    from stock_svr.engine import runner
    from stock_svr.services import trade_analysis as ta

    assert runner.TRADE_ANALYSIS_POLL_SEC is ta.REQUEST_POLL_SEC
    assert runner.HEARTBEAT_SEC <= ta.REQUEST_POLL_SEC <= 30
    assert ta.STALE_PROCESSING_MIN >= 5


def test_엔진_초기화는_요청_처리기를_비워_둔다():
    """처리기는 _startup 에서 계좌 확인과 함께 만들어진다."""
    from types import SimpleNamespace

    from stock_svr.engine.runner import Engine

    engine = Engine(cfg=SimpleNamespace(), db=FakeDb())
    assert engine.trade_analysis is None
