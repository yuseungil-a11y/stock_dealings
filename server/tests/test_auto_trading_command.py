"""웹의 자동거래 시작/중지 명령 큐(`auto_trading_command`) 테스트.

배경: 지금까지 "웹은 조회 전용, 매매 제어는 서버 프로그램(로컬 GUI)에서만"이 원칙이었으나,
관리자 전용 + 실전 통제 재확인(REAL 타이핑)을 전제로 예외를 만든다. 웹쪽 확인 로직은
이 저장소의 범위가 아니다 - 여기서는 서버가 그 명령을 받아 처리하는 부분만 검증한다.

trend_scan_request/company_fetch_request 와 완전히 같은 claim/finish 큐 패턴이다
(claim 은 `UPDATE ... WHERE id=%s AND status='pending'` 의 영향행수로 원자적 판정).

핵심 검증
* `claim_auto_trading_command`: pending 1건 claim 성공(processing 전환), 이미
  processing/done 인 건은 claim 안 됨, 동시 경쟁에서 진 쪽은 처리하지 않음(원자성).
* `finish_auto_trading_command`: 상태/메시지/handled_at 갱신.
* `expire_stale_auto_trading_commands`: 5분 넘은 processing 만 error 로 정리, 최근 것은 유지.
* `Engine._poll_auto_trading_command`: command='start' 성공/실패(이미 실행중) 각각 결과
  메시지, command='stop' 처리, pending 없으면 아무것도 안 함.
* `start_auto_trading`/`stop_auto_trading` 자체의 기존 안전장치(R-11 게이트 검사 등)는
  그대로 재사용될 뿐 이 모듈이 손대지 않는다(별도 회귀는 test_auto_trading.py 가 이미 검증).
"""
from __future__ import annotations

import datetime as _dt

from conftest import FakeDb
from stock_svr.config import AppConfig
from stock_svr.engine.runner import AUTO_TRADING_CMD_POLL_SEC, Engine

NOW = _dt.datetime(2026, 9, 23, 10, 30)


def cmd_db(*, now=None) -> FakeDb:
    db = FakeDb()
    db.now_for_stale = now or NOW
    return db


# ====================================================================== #
# 1. claim — 원자적 경쟁 해결 (trend_scan_request/company_fetch_request 와 동일)
# ====================================================================== #
def test_pending_command_is_claimed_atomically():
    db = cmd_db()
    row_id = db.insert_auto_trading_command("start", "admin")
    first = db.claim_auto_trading_command()
    second = db.claim_auto_trading_command()
    assert first and first["id"] == row_id and first["status"] == "processing"
    assert second is None


def test_claim_ignores_already_processing_command():
    db = cmd_db()
    db.insert_auto_trading_command("start", "admin")
    db.auto_trading_commands[0]["status"] = "processing"
    assert db.claim_auto_trading_command() is None


def test_claim_ignores_already_done_command():
    db = cmd_db()
    db.insert_auto_trading_command("stop", "admin")
    db.auto_trading_commands[0]["status"] = "done"
    assert db.claim_auto_trading_command() is None


def test_claim_race_loser_does_not_process():
    """조회와 UPDATE 사이에 남이 먼저 집으면 영향 행 수가 0 이라 포기한다."""
    db = cmd_db()
    db.insert_auto_trading_command("start", "admin")
    stale = dict(db.auto_trading_commands[0])
    assert db.claim_auto_trading_command() is not None
    db.pending_auto_trading_command = lambda: dict(stale)
    assert db.claim_auto_trading_command() is None


def test_claim_returns_none_when_no_pending():
    db = cmd_db()
    assert db.claim_auto_trading_command() is None


# ====================================================================== #
# 2. finish — 상태/메시지/handled_at 갱신
# ====================================================================== #
def test_finish_updates_status_message_and_handled_at():
    db = cmd_db()
    row_id = db.insert_auto_trading_command("start", "admin")
    db.claim_auto_trading_command()
    db.finish_auto_trading_command(row_id, "done", "자동거래 시작됨")
    row = db.auto_trading_commands[0]
    assert row["status"] == "done"
    assert row["result_message"] == "자동거래 시작됨"
    assert row["handled_at"] is not None


def test_finish_records_error_status_and_message():
    db = cmd_db()
    row_id = db.insert_auto_trading_command("stop", "admin")
    db.claim_auto_trading_command()
    db.finish_auto_trading_command(row_id, "error", "이미 실행 중이거나 시작할 수 없음")
    row = db.auto_trading_commands[0]
    assert row["status"] == "error"
    assert row["result_message"] == "이미 실행 중이거나 시작할 수 없음"


# ====================================================================== #
# 3. stale(처리 중 멈춤) 정리
# ====================================================================== #
def test_expire_stale_commands_marks_long_processing_as_error():
    db = cmd_db(now=NOW)
    db.insert_auto_trading_command("start", "admin")
    db.auto_trading_commands[0].update(
        status="processing", requested_at=NOW - _dt.timedelta(minutes=10))
    n = db.expire_stale_auto_trading_commands(max_minutes=5)
    assert n == 1
    assert db.auto_trading_commands[0]["status"] == "error"
    assert "재시작" in db.auto_trading_commands[0]["result_message"]


def test_expire_stale_commands_ignores_recent_processing():
    db = cmd_db(now=NOW)
    db.insert_auto_trading_command("start", "admin")
    db.auto_trading_commands[0].update(
        status="processing", requested_at=NOW - _dt.timedelta(minutes=2))
    n = db.expire_stale_auto_trading_commands(max_minutes=5)
    assert n == 0
    assert db.auto_trading_commands[0]["status"] == "processing"


def test_expire_stale_commands_ignores_pending_and_done():
    db = cmd_db(now=NOW)
    db.insert_auto_trading_command("start", "admin")
    db.insert_auto_trading_command("stop", "admin")
    db.auto_trading_commands[1].update(
        status="done", requested_at=NOW - _dt.timedelta(minutes=30))
    n = db.expire_stale_auto_trading_commands(max_minutes=5)
    assert n == 0
    assert db.auto_trading_commands[0]["status"] == "pending"
    assert db.auto_trading_commands[1]["status"] == "done"


# ====================================================================== #
# 4. Engine._poll_auto_trading_command
# ====================================================================== #
def _engine(db: FakeDb) -> Engine:
    eng = Engine(AppConfig(), db)
    eng.account_id = 1
    return eng


def test_poll_does_nothing_when_no_pending_command():
    db = cmd_db()
    eng = _engine(db)
    eng._poll_auto_trading_command()  # noqa: SLF001
    assert eng.auto_trading_active is False


def test_poll_start_command_succeeds():
    db = cmd_db()
    eng = _engine(db)
    db.insert_auto_trading_command("start", "admin")

    eng._poll_auto_trading_command()  # noqa: SLF001

    assert eng.auto_trading_active is True
    row = db.auto_trading_commands[0]
    assert row["status"] == "done"
    assert row["result_message"] == "자동거래 시작됨"


def test_poll_start_command_fails_when_already_running():
    db = cmd_db()
    eng = _engine(db)
    assert eng.start_auto_trading(by="테스트") is True
    db.insert_auto_trading_command("start", "admin")

    eng._poll_auto_trading_command()  # noqa: SLF001

    row = db.auto_trading_commands[0]
    assert row["status"] == "error"
    assert row["result_message"] == "이미 실행 중이거나 시작할 수 없음"
    assert eng.auto_trading_active is True   # 기존 실행 상태 그대로 유지


def test_poll_stop_command_reports_open_order_count():
    db = cmd_db()
    db.open_order_codes = {"005930", "000660"}
    eng = _engine(db)
    eng.start_auto_trading(by="테스트")
    db.insert_auto_trading_command("stop", "admin")

    eng._poll_auto_trading_command()  # noqa: SLF001

    assert eng.auto_trading_active is False
    row = db.auto_trading_commands[0]
    assert row["status"] == "done"
    assert "미체결 2건" in row["result_message"]
    assert "자동 취소 안 함" in row["result_message"]


def test_poll_stop_command_works_even_when_not_running():
    db = cmd_db()
    eng = _engine(db)
    db.insert_auto_trading_command("stop", "admin")

    eng._poll_auto_trading_command()  # noqa: SLF001

    assert eng.auto_trading_active is False
    row = db.auto_trading_commands[0]
    assert row["status"] == "done"
    assert "미체결 0건" in row["result_message"]


def test_poll_uses_web_requester_label():
    db = cmd_db()
    eng = _engine(db)
    db.insert_auto_trading_command("start", "관리자1")

    eng._poll_auto_trading_command()  # noqa: SLF001

    assert eng.auto_trading_by == "웹(관리자1)"


def test_poll_processes_only_one_command_per_call():
    db = cmd_db()
    eng = _engine(db)
    db.insert_auto_trading_command("start", "admin")
    db.insert_auto_trading_command("stop", "admin")

    eng._poll_auto_trading_command()  # noqa: SLF001

    assert db.auto_trading_commands[0]["status"] == "done"
    assert db.auto_trading_commands[1]["status"] == "pending"


# ====================================================================== #
# 5. 엔진 조립 확인 (runner.py 배선)
# ====================================================================== #
def test_poll_interval_is_shorter_than_other_request_queues():
    from stock_svr.services.fundamentals_fetch import FETCH_REQUEST_POLL_SEC
    from stock_svr.services.trend_scan import REQUEST_POLL_SEC as TREND_REQUEST_POLL_SEC

    assert AUTO_TRADING_CMD_POLL_SEC < TREND_REQUEST_POLL_SEC
    assert AUTO_TRADING_CMD_POLL_SEC < FETCH_REQUEST_POLL_SEC


def test_engine_polls_auto_trading_command_unconditionally():
    """runner 의 폴링이 auto_trading_active·장 상태와 무관한 독립 분기여야 한다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "stock_svr" / "engine"
           / "runner.py").read_text(encoding="utf-8")
    start = src.index("last_cmd_poll = now_mono")
    block = src[src.rindex("if ", 0, start):start]
    assert "auto_trading_active" not in block
    assert "open_now" not in block
    assert "AUTO_TRADING_CMD_POLL_SEC" in block


def test_startup_expires_stale_commands_once():
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent / "stock_svr"
           / "engine" / "runner.py").read_text(encoding="utf-8")
    assert "expire_stale_auto_trading_commands" in src
