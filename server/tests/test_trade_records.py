"""거래 기록/분석용 보강 기능 테스트.

* `orders` 신호 맥락 컬럼(signal_price / signal_context / params_snapshot)
* `order_event` 주문 상태 변화 이력 (중복 방지 · WS 919 거부사유)
* `event_archive` / `api_error_log` 영구 보관 라우팅
* 보관 정책(파괴 대상 화이트리스트)

실 DB·실 API 는 쓰지 않는다(fake DB / FakeRest / SQL 캡처 대역).
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from conftest import FakeDb, make_ctx
from stock_svr.algo.base import Signal
from stock_svr.config import DbConfig
from stock_svr.db import (
    ARCHIVE_DEDUP_SEC,
    ARCHIVE_RETENTION_MIN,
    PURGEABLE_TABLES,
    Database,
    should_archive_event,
    should_log_api_error,
)
from stock_svr.engine.executor import DRY_RUN_EVENT_MSG, Executor, build_params_snapshot
from stock_svr.kiwoom.errors import KiwoomApiError
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.services.housekeeping import HousekeepingService
from stock_svr.services.sync_orders import (
    NO_REJECT_REASON_MSG,
    OrderSyncService,
    parse_reject_reason,
)

ALGOS = [
    {"code": "risk_guard", "role": "risk", "is_enabled": 1,
     "params": {"max_total_invest": "1000000", "max_invest_per_stock": "300000",
                "stop_loss_pct": "-15", "daily_loss_limit_pct": "-3",
                "max_orders_per_day": "30", "trade_start_time": "09:05",
                "trade_end_time": "15:15", "exchange": "KRX"}},
    {"code": "momentum_screen", "role": "entry", "is_enabled": 1,
     "params": {"top_n": "5", "min_score": "1.5"}},
    {"code": "claude_advisor", "role": "filter", "is_enabled": 1,
     "params": {"model": "claude-opus-5", "min_confidence": "70", "fail_mode": "block"}},
]


def _db_with_algos() -> FakeDb:
    db = FakeDb()
    db.algorithms = [dict(a) for a in ALGOS]
    return db


def _signal(**kw) -> Signal:
    base = dict(algo_code="momentum_screen", stk_cd="005930", stk_nm="삼성전자", side="BUY",
                qty=10, price=60000, trde_tp="0", amount=600000, score=2.5,
                reason="등락률 상위 + 거래량 급증", meta={"cur_prc": 60000, "rank": 3})
    base.update(kw)
    return Signal(**base)


def _real_ctx(db):
    return make_ctx(db, order_enabled=True, trading_mode="real", real_confirm=True)


# ====================================================================== #
# 1. orders 신호 맥락 컬럼
# ====================================================================== #
def test_order_records_signal_context_on_sent_path():
    db = _db_with_algos()
    rest = FakeRest()
    ex = Executor(db, rest, account_id=1, run_id=7)
    res = ex.submit(_real_ctx(db), _signal())

    assert res.sent is True
    row = db.orders[0]
    assert row["signal_price"] == 60000
    ctx_json = json.loads(row["signal_context"])
    assert ctx_json["kind"] == "entry" and ctx_json["algo_code"] == "momentum_screen"
    assert ctx_json["score"] == 2.5
    assert ctx_json["reason"] == "등락률 상위 + 거래량 급증"
    assert ctx_json["meta"]["rank"] == 3
    snap = json.loads(row["params_snapshot"])
    assert snap["risk_guard"]["stop_loss_pct"] == "-15"
    assert snap["source_algo"]["code"] == "momentum_screen"
    assert snap["source_algo"]["params"]["top_n"] == "5"
    assert snap["claude_advisor"] == {"enabled": True, "model": "claude-opus-5",
                                      "min_confidence": "70"}


def test_order_records_signal_context_on_observe_path():
    """관찰모드(SIGNAL_ONLY) 행에도 같은 맥락이 남는다."""
    db = _db_with_algos()
    rest = FakeRest()
    ex = Executor(db, rest, account_id=1)
    res = ex.submit(make_ctx(db), _signal())

    assert res.sent is False and rest.order_call_count == 0
    row = db.orders[0]
    assert row["is_dry_run"] == 1 and row["status"] == "SIGNAL_ONLY"
    assert row["signal_price"] == 60000
    assert json.loads(row["signal_context"])["kind"] == "entry"
    assert json.loads(row["params_snapshot"])["risk_guard"]["max_total_invest"] == "1000000"


def test_signal_price_falls_back_to_meta_and_context_price():
    """시장가 신호는 신호가 들고 온 현재가(meta) → 보유 현재가 순으로 기준가를 잡는다."""
    db = _db_with_algos()
    ex = Executor(db, FakeRest(), account_id=1)
    ex.submit(_real_ctx(db), _signal(price=None, trde_tp="3", meta={"cur_prc": 58000}))
    assert db.orders[-1]["signal_price"] == 58000

    db2 = _db_with_algos()
    ctx = make_ctx(db2, order_enabled=True, trading_mode="real", real_confirm=True,
                   holdings={"005930": {"stk_cd": "005930", "rmnd_qty": 5, "cur_prc": 57000}})
    Executor(db2, FakeRest(), account_id=1).submit(
        ctx, _signal(price=None, trde_tp="3", meta={}))
    assert db2.orders[-1]["signal_price"] == 57000


def test_reason_is_truncated_in_signal_context():
    db = _db_with_algos()
    ex = Executor(db, FakeRest(), account_id=1)
    ex.submit(_real_ctx(db), _signal(reason="가" * 5000))
    assert len(json.loads(db.orders[-1]["signal_context"])["reason"]) == 2000


def test_snapshot_never_contains_secrets():
    """비밀·키·계좌번호는 신호 맥락에도 파라미터 스냅샷에도 남지 않는다."""
    db = _db_with_algos()
    db.algorithms.append({
        "code": "shady", "role": "entry", "is_enabled": 1,
        "params": {"appkey": "AK-LIVE-1234", "secretkey": "SK-LIVE-9999",
                   "account_no": "1234567890", "safe_param": "7"}})
    ex = Executor(db, FakeRest(), account_id=1)
    ex.submit(_real_ctx(db), _signal(
        algo_code="shady", meta={"token": "abc123", "계좌번호": "1234567890", "step": 2}))

    row = db.orders[-1]
    blob = f"{row['signal_context']}{row['params_snapshot']}"
    for secret in ("AK-LIVE-1234", "SK-LIVE-9999", "1234567890", "abc123"):
        assert secret not in blob
    snap = json.loads(row["params_snapshot"])
    assert snap["source_algo"]["params"] == {"safe_param": "7"}
    assert json.loads(row["signal_context"])["meta"] == {"step": 2}


def test_params_snapshot_survives_missing_algorithms():
    """알고리즘 조회가 실패해도(빈 목록) 주문 기록은 계속된다."""
    out = json.loads(build_params_snapshot([], "momentum_screen"))
    assert out["risk_guard"] == {}
    assert out["source_algo"] == {"code": "momentum_screen", "enabled": None, "params": {}}
    assert out["claude_advisor"]["enabled"] is False


def test_order_context_missing_when_algo_load_fails():
    db = _db_with_algos()
    db.fail_on.add("load_algorithms")
    ex = Executor(db, FakeRest(), account_id=1)
    res = ex.submit(_real_ctx(db), _signal())
    assert res.sent is True
    assert json.loads(db.orders[-1]["params_snapshot"])["risk_guard"] == {}


# ====================================================================== #
# 2. order_event — Executor 경로
# ====================================================================== #
def _types(db: FakeDb, order_id: int) -> list[str]:
    return [e["event_type"] for e in db.order_events_of(order_id)]


def test_executor_events_created_then_sent():
    db = _db_with_algos()
    ex = Executor(db, FakeRest(), account_id=1)
    res = ex.submit(_real_ctx(db), _signal())

    assert _types(db, res.order_id) == ["CREATED", "SENT"]
    created, sent = db.order_events_of(res.order_id)
    assert created["status"] == "SENT" and created["remain_qty"] == 10
    assert created["source"] == "EXECUTOR" and created["account_id"] == 1
    assert sent["ord_no"] == res.ord_no and sent["return_code"] == 0


def test_executor_dry_run_event_notes_observe_mode():
    db = _db_with_algos()
    ex = Executor(db, FakeRest(), account_id=1)
    res = ex.submit(make_ctx(db), _signal())

    events = db.order_events_of(res.order_id)
    assert _types(db, res.order_id) == ["CREATED"]
    assert events[0]["status"] == "SIGNAL_ONLY"
    assert DRY_RUN_EVENT_MSG in events[0]["message"]


def test_executor_events_created_then_failed_on_rejection():
    """거래소 거부(return_code != 0) → CREATED→FAILED + 거부사유 저장."""
    db = _db_with_algos()
    rest = FakeRest()
    rest.raise_on["kt10000"] = KiwoomApiError("kt10000", 3, "주문가능금액 부족")
    ex = Executor(db, rest, account_id=1)
    res = ex.submit(_real_ctx(db), _signal())

    assert res.sent is False and res.unknown_state is False
    assert _types(db, res.order_id) == ["CREATED", "FAILED"]
    failed = db.order_events_of(res.order_id)[-1]
    assert failed["return_code"] == 3
    assert "주문가능금액 부족" in failed["reject_reason"]
    assert "주문가능금액 부족" in db.orders[-1]["reject_reason"]


def test_executor_unknown_state_adds_unknown_event():
    db = _db_with_algos()
    rest = FakeRest()
    rest.raise_on["kt10000"] = TimeoutError("응답 없음")
    ex = Executor(db, rest, account_id=1)
    res = ex.submit(_real_ctx(db), _signal())

    assert res.unknown_state is True
    assert _types(db, res.order_id) == ["CREATED", "FAILED", "UNKNOWN"]
    assert db.orders[-1].get("reject_reason") is None


def test_cancel_path_records_events():
    db = _db_with_algos()
    ex = Executor(db, FakeRest(), account_id=1)
    res = ex.submit_cancel(_real_ctx(db), "0001", "005930", 4)
    assert res.sent is True
    assert _types(db, res.order_id) == ["CREATED", "SENT"]


def test_order_event_failure_does_not_break_order():
    """이벤트 기록 실패는 주문 처리를 깨뜨리지 않는다(로그만)."""
    db = _db_with_algos()
    db.fail_on.add("insert_order_event")
    ex = Executor(db, FakeRest(), account_id=1)
    res = ex.submit(_real_ctx(db), _signal())

    assert res.sent is True and res.error is None
    assert db.order_events == []
    assert db.orders[-1]["status"] == "SENT"


# ====================================================================== #
# 3. order_event — 동기화(WS/REST) 경로
# ====================================================================== #
def _ws(ord_no="0007", ord_qty=10, oso=10, cntr_qty=0, stt="접수", reject=None, pric=0):
    values = {"9203": ord_no, "9001": "A005930", "302": "삼성전자", "907": "2",
              "900": str(ord_qty), "902": str(oso), "911": str(cntr_qty),
              "910": f"+{pric}" if pric else "0", "909": f"C{cntr_qty}",
              "913": stt, "908": "093015", "906": "0"}
    if reject is not None:
        values["919"] = reject
    return values


def test_ws_event_sequence_accepted_partial_filled():
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1)
    svc.on_order_exec(_ws(oso=10, cntr_qty=0, stt="접수"))
    svc.on_order_exec(_ws(oso=4, cntr_qty=6, stt="부분체결", pric=60000))
    svc.on_order_exec(_ws(oso=0, cntr_qty=10, stt="체결", pric=60000))

    assert [e["event_type"] for e in db.order_events] == ["ACCEPTED", "PARTIAL", "FILLED"]
    assert [e["filled_qty"] for e in db.order_events] == [0, 6, 10]
    assert [e["remain_qty"] for e in db.order_events] == [10, 4, 0]
    assert db.order_events[-1]["price"] == 60000
    assert all(e["source"] == "WS" for e in db.order_events)
    assert db.order_events[0]["ord_no"] == "0007"


def test_ws_no_event_when_nothing_changed():
    """같은 내용이 다시 와도 이벤트를 만들지 않는다(중복 방지)."""
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1)
    msg = _ws(oso=4, cntr_qty=6, stt="부분체결", pric=60000)
    svc.on_order_exec(msg)
    svc.on_order_exec(dict(msg))
    svc.on_order_exec(dict(msg))

    assert len(db.order_events) == 1


def test_ws_rejected_stores_reject_reason_everywhere():
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1)
    svc.on_order_exec(_ws(oso=10, stt="거부", reject="증거금 부족"))

    ev = db.order_events[-1]
    assert ev["event_type"] == "REJECTED" and ev["reject_reason"] == "증거금 부족"
    assert db.orders[-1]["reject_reason"] == "증거금 부족"


def test_ws_rejected_without_reason_notes_missing():
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1)
    svc.on_order_exec(_ws(oso=10, stt="거부", reject="0"))

    ev = db.order_events[-1]
    assert ev["event_type"] == "REJECTED" and ev["reject_reason"] is None
    assert ev["message"] == NO_REJECT_REASON_MSG
    assert db.orders[-1].get("reject_reason") is None


def test_ws_repeated_rejection_without_919_makes_no_extra_event():
    """이미 사유가 저장된 거부 주문이 919 없이 다시 와도 이벤트가 늘지 않는다."""
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest(), 1)
    svc.on_order_exec(_ws(oso=10, stt="거부", reject="증거금 부족"))
    svc.on_order_exec(_ws(oso=10, stt="거부"))
    svc.on_order_exec(_ws(oso=10, stt="거부", reject="0"))
    assert len(db.order_events) == 1

    # 사유가 새로 들어오면 그때는 이벤트를 남긴다
    svc.on_order_exec(_ws(oso=10, stt="거부", reject="호가 단위 오류"))
    assert len(db.order_events) == 2
    assert db.order_events[-1]["reject_reason"] == "호가 단위 오류"


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None), ("0", None), (0, None),
    ("증거금 부족", "증거금 부족"), ("  호가 단위 오류  ", "호가 단위 오류"),
    ("사" * 400, "사" * 255),
])
def test_parse_reject_reason_matrix(raw, expected):
    assert parse_reject_reason(raw) == expected


def test_ws_event_failure_does_not_break_sync():
    db = FakeDb()
    db.fail_on.add("insert_order_event")
    svc = OrderSyncService(db, FakeRest(), 1)
    svc.on_order_exec(_ws(oso=4, cntr_qty=6, stt="부분체결", pric=60000))

    assert db.order_events == []
    assert db.orders[-1]["filled_qty"] == 6      # 동기화 자체는 정상 수행


OSO_PAGE = {
    "return_code": 0,
    "oso": [{"ord_no": "0001", "stk_cd": "A005930", "stk_nm": "삼성전자",
             "ord_qty": "000000010", "oso_qty": "000000004", "ord_pric": "+000060000",
             "ord_stt": "접수", "io_tp_nm": "현금매수", "trde_tp": "보통",
             "stex_tp_txt": "KRX"}],
}


def test_rest_open_order_sync_emits_event_once():
    db = FakeDb()
    svc = OrderSyncService(db, FakeRest({"ka10075": OSO_PAGE}), 1)
    svc.sync_open_orders()
    svc.sync_open_orders()       # 두 번째는 변화가 없다

    assert len(db.order_events) == 1
    ev = db.order_events[0]
    assert ev["event_type"] == "ACCEPTED" and ev["source"] == "REST"
    assert ev["filled_qty"] == 6 and ev["remain_qty"] == 4


# ====================================================================== #
# 4. 영구 보관 라우팅 (event_archive / api_error_log)
# ====================================================================== #
class RecordingDb(Database):
    """SQL 만 캡처하는 Database 대역(커넥션을 만들지 않는다)."""

    def __init__(self):
        super().__init__(DbConfig(host="127.0.0.1", port=4406, name="t", user="stock_svr"))
        self.sqls: list[tuple[str, tuple]] = []
        self.settings: dict[str, str] = {}

    def insert(self, sql, args=None):
        self.sqls.append((" ".join(sql.split()), tuple(args or ())))
        return len(self.sqls)

    def execute(self, sql, args=None):
        self.sqls.append((" ".join(sql.split()), tuple(args or ())))
        return 1

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def tables_touched(self, verb: str) -> list[str]:
        out = []
        for sql, _ in self.sqls:
            parts = sql.split()
            if parts and parts[0].upper() == verb.upper():
                out.append(parts[2] if verb.upper() == "DELETE" else parts[2])
        return out

    def archived(self) -> list[tuple]:
        return [a for s, a in self.sqls if "INSERT INTO event_archive" in s]

    def api_errors(self) -> list[tuple]:
        return [a for s, a in self.sqls if "INSERT INTO api_error_log" in s]


@pytest.mark.parametrize("level,category,expected", [
    ("ERROR", "system", True),
    ("WARN", "sync", True),
    ("ERROR", "ws", True),
    ("INFO", "order", True),
    ("INFO", "algo", True),
    ("INFO", "engine", True),
    ("INFO", "risk", True),
    ("INFO", "sync", False),
    ("INFO", "system", False),
    ("DEBUG", "ws", False),
])
def test_archive_routing_rules(level, category, expected):
    assert should_archive_event(level, category, "주문 전송 완료") is expected


@pytest.mark.parametrize("message", ["heartbeat 10:00:00", "미체결 3건 동기화", "하트비트 갱신"])
def test_noisy_info_is_not_archived(message):
    assert should_archive_event("INFO", "engine", message) is False
    # 같은 문구라도 WARN/ERROR 면 보관한다
    assert should_archive_event("ERROR", "engine", message) is True


def test_log_event_writes_both_tables():
    db = RecordingDb()
    db.log_event("ERROR", "order", "주문 전송 실패: 증거금 부족")
    assert len(db.archived()) == 1
    assert db.archived()[0] == ("ERROR", "order", "주문 전송 실패: 증거금 부족")


def test_log_event_skips_archive_for_routine_info():
    db = RecordingDb()
    db.log_event("INFO", "sync", "미체결 3건 동기화")
    assert db.archived() == []
    assert any("INSERT INTO event_log" in s for s, _ in db.sqls)


def test_archive_deduplicates_within_window():
    db = RecordingDb()
    for _ in range(5):
        db.log_event("ERROR", "order", "같은 오류")
    assert len(db.archived()) == 1
    # 창이 지나면 다시 1건 남는다
    key = ("ERROR", "order", "같은 오류")
    db._archive_seen[key] -= _dt.timedelta(seconds=ARCHIVE_DEDUP_SEC + 1)   # noqa: SLF001
    db.log_event("ERROR", "order", "같은 오류")
    assert len(db.archived()) == 2
    # 다른 메시지는 억제되지 않는다
    db.log_event("ERROR", "order", "다른 오류")
    assert len(db.archived()) == 3


def test_archive_failure_does_not_break_event_log():
    class Broken(RecordingDb):
        def execute(self, sql, args=None):
            if "event_archive" in sql:
                raise RuntimeError("아카이브 테이블 없음")
            return super().execute(sql, args)

    db = Broken()
    assert db.log_event("ERROR", "order", "치명 오류") > 0


@pytest.mark.parametrize("status,rc,expected", [
    (200, 0, False), (200, None, False), (200, 3, True), (200, "3", True),
    (400, 0, True), (500, None, True), (401, 8005, True), (None, 1700, True),
    (200, "abc", True),
])
def test_api_error_condition(status, rc, expected):
    assert should_log_api_error(status, rc) is expected


def test_log_api_call_archives_only_errors():
    db = RecordingDb()
    db.log_api_call("kt00018", 200, 0, "정상", 120)
    assert db.api_errors() == []
    db.log_api_call("ka10075", 500, None, "서버 오류", 900)
    assert db.api_errors() == [("ka10075", 500, None, "서버 오류", 900)]


def test_api_error_failure_does_not_break_api_call_log():
    class Broken(RecordingDb):
        def execute(self, sql, args=None):
            if "api_error_log" in sql:
                raise RuntimeError("테이블 없음")
            return super().execute(sql, args)

    db = Broken()
    db.log_api_call("ka10075", 500, None, "서버 오류", 900)
    assert any("INSERT INTO api_call_log" in s for s, _ in db.sqls)


# ====================================================================== #
# 5. 보관 정책 — 무엇을 지우고 무엇을 절대 지우지 않는가
# ====================================================================== #
PROTECTED_TABLES = ("order_event", "orders", "executions", "signal_log",
                    "llm_decision_log", "position_state")


def test_purge_old_targets_only_short_term_logs():
    db = RecordingDb()
    db.purge_old(7)
    deleted = [s.split()[2] for s, _ in db.sqls if s.startswith("DELETE")]
    assert deleted == ["event_log", "api_call_log", "screening_result"]


def test_purge_archives_targets_only_archives():
    db = RecordingDb()
    db.settings["archive_retention_days"] = "365"
    db.purge_archives()
    deleted = [(s.split()[2], a[0]) for s, a in db.sqls if s.startswith("DELETE")]
    assert deleted == [("event_archive", 365), ("api_error_log", 365)]


@pytest.mark.parametrize("raw,expected", [
    ("365", 365), ("30", 30), ("7", ARCHIVE_RETENTION_MIN), ("", 365), (None, 365),
    ("abc", 365), ("1000", 1000),
])
def test_archive_retention_floor_and_default(raw, expected):
    db = RecordingDb()
    if raw is not None:
        db.settings["archive_retention_days"] = raw
    assert db.archive_retention_days() == expected


def test_no_purge_sql_touches_trade_ledger_tables():
    """파괴 대상 화이트리스트 고정 — 거래 원장에는 DELETE 가 없어야 한다."""
    db = RecordingDb()
    db.purge_old(7)
    db.purge_archives(365)
    for sql, _ in db.sqls:
        if not sql.startswith("DELETE"):
            continue
        table = sql.split()[2]
        assert table in PURGEABLE_TABLES
        assert table not in PROTECTED_TABLES


def test_purge_source_has_no_delete_for_protected_tables():
    """코드 전체에 보호 테이블 대상 DELETE 문이 없음을 고정한다."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "stock_svr"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for table in PROTECTED_TABLES:
            assert f"DELETE FROM {table}" not in text, f"{path.name}: {table}"


def test_housekeeping_runs_both_purges():
    db = FakeDb()
    db.settings["archive_retention_days"] = "365"
    house = HousekeepingService(db, None, 1)
    out = house.run_purge(7, force=True)

    assert dict(db.purged) == {"event_log": 7, "api_call_log": 7, "screening_result": 28,
                               "event_archive": 365, "api_error_log": 365}
    assert set(out) >= {"event_log", "event_archive", "api_error_log"}


def test_housekeeping_purge_survives_archive_failure():
    db = FakeDb()
    db.fail_on.add("purge_archives")
    house = HousekeepingService(db, None, 1)
    out = house.run_purge(7, force=True)
    assert "event_log" in out and "event_archive" not in out


def test_housekeeping_purge_runs_once_a_day():
    db = FakeDb()
    house = HousekeepingService(db, None, 1)
    assert house.run_purge(7) != {}
    assert house.run_purge(7) == {}
