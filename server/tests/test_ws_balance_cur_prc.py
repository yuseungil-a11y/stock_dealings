"""WS `04` 잔고 실시간(on_balance)의 필드 누락 처리 회귀 테스트 (2026-10).

키움 잔고 실시간 메시지는 부분 갱신(수량 변동 등)일 때 일부 필드를 빼고 올 수 있다.
예전엔 필드가 없으면 0/종목코드로 기록해 기존 값을 덮어썼다.

- 현재가("10", v1.18.6): 0 으로 기록 → 정상 종목이 '거래불가(현재가 0)'로 오판, 투입 한도에서 누락
- 보유수량("930", v1.18.7): 0 으로 보고 행 DELETE → 아직 보유 중인 종목이 사라져 중복 매수 위험
- 매입단가("931", v1.18.7): 0 으로 덮어씀
- 종목명("302", v1.18.7): 종목코드로 덮어씀 → '(폐)' 표기(거래불가 판정 근거) 소실

이제는
- 필드 없음 → None(모름): 기존 값 유지(COALESCE). 수량이 없으면 holding 을 아예 건드리지 않는다.
- 실제 "0" 수신 → 0 으로 기록 / 수량 0 이면 전량 청산으로 보고 DELETE(진짜 신호는 그대로 살린다)

holding 테이블 upsert 는 아래 미니 에뮬레이터로 MySQL 의 INSERT ... ON DUPLICATE KEY UPDATE
의미(col=VALUES(col), col=COALESCE(VALUES(col), col), col=COALESCE(%s, col))를 그대로 흉내 내
검증한다. NOT NULL 컬럼(stk_nm, rmnd_qty)에 None 을 INSERT 하면 MySQL 처럼 오류로 본다.
"""
from __future__ import annotations

import re
from decimal import Decimal

from conftest import FakeDb, FakeMarket, make_ctx

from stock_svr.algo.momentum_screen import MomentumScreen
from stock_svr.algo.params import ParamSet
from stock_svr.engine.context import reset_untradable_log_state
from stock_svr.kiwoom.fake import FakeRest
from stock_svr.services.sync_account import AccountService
from stock_svr.services.sync_orders import OrderSyncService

NOT_NULL_COLS = ("account_id", "stk_cd", "stk_nm", "rmnd_qty")

ACCOUNT = 1


class HoldingTableDb(FakeDb):
    """holding 테이블 upsert/delete 만 메모리에서 MySQL 의미대로 흉내 낸다."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.table: dict[tuple, dict] = {}

    def seed(self, stk_cd: str, **cols) -> None:
        row = {"account_id": ACCOUNT, "stk_cd": stk_cd, "stk_nm": stk_cd, "rmnd_qty": 1,
               "trde_able_qty": None, "pur_pric": None, "cur_prc": None, "pur_amt": None}
        row.update(cols)
        self.table[(ACCOUNT, stk_cd)] = row

    def execute(self, sql, args=None):
        if sql.startswith("DELETE FROM holding"):
            self.table.pop((args[0], args[1]), None)
            return 1
        m = re.match(r"INSERT INTO holding \(([^)]*)\)", sql)
        assert m, f"예상하지 못한 SQL: {sql}"
        cols = [c.strip() for c in m.group(1).split(",")]
        update = sql.split("ON DUPLICATE KEY UPDATE", 1)[1]
        n_extra = update.count("%s")
        assert len(cols) + n_extra == len(args), "자리표시자 수와 바인딩 파라미터 수가 다르다"
        new = dict(zip(cols, args[:len(cols)]))
        extra = list(args[len(cols):])
        for c in NOT_NULL_COLS:
            # MySQL 은 중복 키 여부와 무관하게 NOT NULL 컬럼에 NULL INSERT 를 거부한다(1048).
            assert new.get(c) is not None, f"NOT NULL 컬럼 {c} 에 NULL INSERT"
        key = (new["account_id"], new["stk_cd"])
        old = self.table.get(key)
        if old is None:
            self.table[key] = new
            return 1
        for assign in re.split(r",\s*(?![^()]*\))", update):
            col, expr = (s.strip() for s in assign.split("=", 1))
            m_val = re.fullmatch(r"VALUES\((\w+)\)", expr)
            m_coal = re.fullmatch(r"COALESCE\(VALUES\((\w+)\),\s*(\w+)\)", expr)
            m_param = re.fullmatch(r"COALESCE\(%s,\s*(\w+)\)", expr)
            if m_val:
                old[col] = new[m_val.group(1)]
            elif m_coal:
                v = new[m_coal.group(1)]
                old[col] = v if v is not None else old.get(m_coal.group(2))
            elif m_param:
                v = extra.pop(0)
                old[col] = v if v is not None else old.get(m_param.group(1))
            else:
                raise AssertionError(f"에뮬레이터가 모르는 UPDATE 식: {expr}")
        assert not extra, "UPDATE 절 파라미터가 남았다"
        return 1

    def row(self, stk_cd: str) -> dict | None:
        return self.table.get((ACCOUNT, stk_cd))


def svc(db) -> OrderSyncService:
    return OrderSyncService(db, FakeRest(), ACCOUNT)


def ctx_from(db: HoldingTableDb):
    holdings = {r["stk_cd"]: dict(r) for r in db.table.values()}
    return make_ctx(FakeDb(), holdings=holdings)


def setup_function(_):
    reset_untradable_log_state()


# ---------------------------------------------------------------------- #
def test_missing_price_field_preserves_known_price():
    """현재가 필드 없는 부분 갱신이 기존 정상 현재가를 0/NULL 로 덮어쓰지 않는다."""
    db = HoldingTableDb()
    db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000, cur_prc=63_000,
            pur_amt=600_000)
    svc(db).on_balance({"9001": "A005930", "302": "삼성전자", "930": "12", "933": "12",
                        "931": "+60500"})
    row = db.row("005930")
    assert row["cur_prc"] == 63_000
    assert row["rmnd_qty"] == 12                       # 다른 컬럼은 정상 갱신
    assert row["pur_pric"] == 60_500


def test_empty_price_field_also_preserves_known_price():
    db = HoldingTableDb()
    db.seed("005930", cur_prc=63_000)
    svc(db).on_balance({"9001": "A005930", "930": "5", "10": ""})
    assert db.row("005930")["cur_prc"] == 63_000


def test_present_price_overwrites_and_is_absolute():
    db = HoldingTableDb()
    db.seed("005930", cur_prc=63_000)
    svc(db).on_balance({"9001": "A005930", "930": "5", "10": "-61500"})
    assert db.row("005930")["cur_prc"] == 61_500


def test_real_zero_price_is_still_recorded_as_zero():
    """키움이 실제로 '0' 을 내려준 경우는 0 으로 기록한다(진짜 신호는 유지)."""
    for raw in ("0", 0, "+000000000"):
        db = HoldingTableDb()
        db.seed("040670", stk_nm="와이즈파워", cur_prc=500)
        svc(db).on_balance({"9001": "A040670", "930": "200", "10": raw})
        assert db.row("040670")["cur_prc"] == 0, raw
    ctx = ctx_from(db)
    assert "현재가 0" in ctx.untradable_reason("040670")


def test_new_row_with_missing_price_is_null_and_not_untradable():
    """기존 행이 없는 종목에 현재가 없는 메시지 → NULL 로 저장, 거래불가로 판정하지 않는다."""
    db = HoldingTableDb()
    svc(db).on_balance({"9001": "A181710", "302": "NHN", "930": "1", "933": "1",
                        "931": "58400"})
    row = db.row("181710")
    assert row is not None and row["cur_prc"] is None
    ctx = ctx_from(db)
    assert ctx.untradable_reason("181710", "NHN") == ""


def test_missing_price_does_not_drop_holding_from_invest_limit():
    """종단: 현재가 필드 누락 메시지 뒤에도 정상 종목의 매입금액이 투입 한도 합계에 남는다.

    예전 기록 방식(0 으로 덮어씀)이었다면 이 종목은 '거래불가(현재가 0)'로 판정돼
    total_invested() 에서 빠졌다 - 그 결과를 함께 고정해 둔다.
    """
    db = HoldingTableDb()
    db.seed("181710", stk_nm="NHN", rmnd_qty=1, pur_pric=58_400, cur_prc=56_400,
            pur_amt=58_400)
    db.seed("083450", stk_nm="GST", rmnd_qty=1, pur_pric=52_700, cur_prc=55_300,
            pur_amt=52_700)
    svc(db).on_balance({"9001": "A181710", "302": "NHN", "930": "1", "933": "1",
                        "931": "58400"})                     # "10" 없음
    ctx = ctx_from(db)
    assert ctx.untradable_reason("181710", "NHN") == ""
    assert ctx.invested_in("181710") == 58_400
    assert ctx.total_invested() == 58_400 + 52_700

    # 예전 동작(현재가 0 으로 덮어씀) 재현: 실제 돈이 한도 계산에서 사라졌다
    reset_untradable_log_state()
    buggy = {k: dict(v) for k, v in db.table.items()}
    buggy[(ACCOUNT, "181710")]["cur_prc"] = 0
    old_ctx = make_ctx(FakeDb(), holdings={r["stk_cd"]: r for r in buggy.values()})
    assert old_ctx.total_invested() == 52_700


def test_upsert_sql_keeps_existing_price_via_coalesce():
    """SQL 자체도 고정: cur_prc 는 COALESCE 로만 갱신하고, 파라미터 마지막 자리가 None 이다."""
    calls = []

    class D(FakeDb):
        def execute(self, sql, args=None):
            calls.append((sql, args))
            return 1

    svc(D()).on_balance({"9001": "A005930", "930": "3"})
    sql, args = calls[0]
    assert "cur_prc=COALESCE(VALUES(cur_prc), cur_prc)" in sql
    assert "cur_prc=VALUES(cur_prc)" not in sql
    assert args[6] is None                              # cur_prc 자리


# ====================================================================== #
# 보유수량(930) — 필드 누락 vs 실제 0
# ====================================================================== #
def test_missing_qty_field_does_not_delete_holding():
    """보유수량 필드 없는 부분 갱신이 보유종목 행을 지우지 않는다(값도 전혀 바꾸지 않는다)."""
    for msg in ({"9001": "A005930", "10": "63500"},          # 930 키 자체가 없음
                {"9001": "A005930", "930": ""},               # 빈 문자열
                {"9001": "A005930", "930": "   "},            # 공백
                {"9001": "A005930", "930": None},             # 명시적 null
                {"9001": "A005930", "930": "abc"}):           # 숫자 아님
        db = HoldingTableDb()
        db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, trde_able_qty=10,
                pur_pric=60_000, cur_prc=63_000, pur_amt=600_000)
        before = dict(db.row("005930"))
        svc(db).on_balance(msg)
        assert db.row("005930") == before, msg


def test_missing_qty_field_issues_no_sql_at_all():
    calls = []

    class D(FakeDb):
        def execute(self, sql, args=None):
            calls.append((sql, args))
            return 1

    svc(D()).on_balance({"9001": "A005930", "302": "삼성전자", "10": "63000"})
    assert calls == []


def test_real_zero_qty_still_deletes_holding():
    """[안전 핵심] 실제 '0' 수신(전량 매도)은 지금도 행을 DELETE 한다 - 청산 감지 회귀 금지."""
    for raw in ("0", 0, "000000000", "+000000000", "-000000000", " 0 "):
        db = HoldingTableDb()
        db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000, cur_prc=63_000,
                pur_amt=600_000)
        db.seed("000660", stk_nm="SK하이닉스", rmnd_qty=2, pur_pric=120_000,
                cur_prc=121_000, pur_amt=240_000)
        svc(db).on_balance({"9001": "A005930", "302": "삼성전자", "930": raw, "10": "63000"})
        assert db.row("005930") is None, raw
        assert db.row("000660") is not None, raw           # 다른 종목은 그대로
        ctx = ctx_from(db)
        assert "005930" not in ctx.holdings
        assert ctx.total_invested() == 240_000


def test_real_zero_qty_delete_sql_targets_account_and_code():
    calls = []

    class D(FakeDb):
        def execute(self, sql, args=None):
            calls.append((sql, args))
            return 1

    svc(D()).on_balance({"9001": "A005930", "930": "0"})
    assert len(calls) == 1
    sql, args = calls[0]
    assert sql.startswith("DELETE FROM holding WHERE account_id=%s AND stk_cd=%s")
    assert args == (ACCOUNT, "005930")


def test_negative_qty_is_ignored_not_deleted():
    """음수 수량은 정상 신호가 아니다 - 삭제(위험한 방향)하지 않고 무시한다."""
    db = HoldingTableDb()
    db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000)
    before = dict(db.row("005930"))
    svc(db).on_balance({"9001": "A005930", "930": "-5"})
    assert db.row("005930") == before


def test_missing_qty_on_unknown_stock_creates_no_row():
    """기존 행 없는 종목 + 수량 없음 → 행을 만들지 않는다(NOT NULL rmnd_qty 에 NULL 금지)."""
    db = HoldingTableDb()
    svc(db).on_balance({"9001": "A181710", "302": "NHN", "931": "58400"})
    assert db.row("181710") is None


def test_positive_qty_still_updates():
    db = HoldingTableDb()
    db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000)
    svc(db).on_balance({"9001": "A005930", "930": "+000000007", "933": "7"})
    row = db.row("005930")
    assert row["rmnd_qty"] == 7 and row["trde_able_qty"] == 7
    assert row["pur_pric"] == 60_000 and row["stk_nm"] == "삼성전자"


# ====================================================================== #
# 매입단가(931)
# ====================================================================== #
def test_missing_purchase_price_preserves_known_price():
    for msg in ({"9001": "A005930", "930": "12"},
                {"9001": "A005930", "930": "12", "931": ""},
                {"9001": "A005930", "930": "12", "931": None}):
        db = HoldingTableDb()
        db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000, cur_prc=63_000)
        svc(db).on_balance(msg)
        row = db.row("005930")
        assert row["pur_pric"] == 60_000, msg
        assert row["rmnd_qty"] == 12, msg


def test_present_purchase_price_overwrites_and_is_absolute():
    db = HoldingTableDb()
    db.seed("005930", pur_pric=60_000)
    svc(db).on_balance({"9001": "A005930", "930": "12", "931": "-000061000"})
    assert db.row("005930")["pur_pric"] == 61_000


def test_real_zero_purchase_price_is_recorded_as_zero():
    db = HoldingTableDb()
    db.seed("005930", pur_pric=60_000)
    svc(db).on_balance({"9001": "A005930", "930": "12", "931": "0"})
    assert db.row("005930")["pur_pric"] == 0


def test_new_row_with_missing_purchase_price_is_null():
    db = HoldingTableDb()
    svc(db).on_balance({"9001": "A181710", "302": "NHN", "930": "1"})
    assert db.row("181710")["pur_pric"] is None


def test_upsert_sql_uses_coalesce_for_pur_pric_and_stk_nm():
    calls = []

    class D(FakeDb):
        def execute(self, sql, args=None):
            calls.append((sql, args))
            return 1

    svc(D()).on_balance({"9001": "A005930", "930": "3"})
    sql, args = calls[0]
    assert "pur_pric=COALESCE(VALUES(pur_pric), pur_pric)" in sql
    assert "pur_pric=VALUES(pur_pric)" not in sql
    assert "stk_nm=COALESCE(%s, stk_nm)" in sql
    assert "stk_nm=VALUES(stk_nm)" not in sql
    assert args[2] == "005930"                          # INSERT 용 대체 이름(NOT NULL)
    assert args[5] is None                              # pur_pric 자리
    assert args[7] is None                              # UPDATE 용 이름: 없음 → 기존 유지


# ====================================================================== #
# 종목명(302)
# ====================================================================== #
def test_missing_name_preserves_known_name():
    for msg in ({"9001": "A005930", "930": "12"},
                {"9001": "A005930", "930": "12", "302": ""},
                {"9001": "A005930", "930": "12", "302": "   "},
                {"9001": "A005930", "930": "12", "302": None}):
        db = HoldingTableDb()
        db.seed("005930", stk_nm="삼성전자", rmnd_qty=10)
        svc(db).on_balance(msg)
        assert db.row("005930")["stk_nm"] == "삼성전자", msg


def test_missing_name_keeps_delisted_marker_and_untradable_detection():
    """'(폐)' 표기가 종목명 누락 메시지로 지워지지 않고, 거래불가 판정도 유지된다."""
    db = HoldingTableDb()
    db.seed("040670", stk_nm="(폐)와이즈파워", rmnd_qty=200, pur_pric=500, cur_prc=500,
            pur_amt=100_000)
    svc(db).on_balance({"9001": "A040670", "930": "200", "933": "200"})
    assert db.row("040670")["stk_nm"] == "(폐)와이즈파워"
    ctx = ctx_from(db)
    assert "상장폐지" in ctx.untradable_reason("040670")


def test_present_name_overwrites():
    db = HoldingTableDb()
    db.seed("005930", stk_nm="옛이름")
    svc(db).on_balance({"9001": "A005930", "930": "1", "302": " 삼성전자 "})
    assert db.row("005930")["stk_nm"] == "삼성전자"


def test_new_row_with_missing_name_falls_back_to_code():
    """기존 이름이 없는 새 행은 종목코드를 이름으로 쓴다(stk_nm NOT NULL)."""
    db = HoldingTableDb()
    svc(db).on_balance({"9001": "A181710", "930": "1"})
    assert db.row("181710")["stk_nm"] == "181710"


def test_new_row_with_name_uses_it():
    db = HoldingTableDb()
    svc(db).on_balance({"9001": "A181710", "930": "1", "302": "NHN"})
    assert db.row("181710")["stk_nm"] == "NHN"


# ====================================================================== #
# 종단: 수량 누락 메시지가 중복 매수·투입 한도 누락을 일으키지 않는다
# ====================================================================== #
MOM_DEFS = [{"param_key": k, "label": k, "value_type": t, "default_value": d,
             "enum_options": eo} for k, t, d, eo in (
    ("market", "enum", "000", "000:전체,001:코스피,101:코스닥"),
    ("min_flu_rt", "decimal", "3", None),
    ("max_flu_rt", "decimal", "15", None),
    ("min_volume_surge_rt", "decimal", "100", None),
    ("min_trde_qty", "int", "100000", None),
    ("min_price", "int", "1000", None),
    ("exclude_etf", "bool", "1", None),
    ("top_n", "int", "5", None),
    ("buy_amount", "int", "100000", None),
    ("max_new_per_day", "int", "3", None),
    ("order_type", "enum", "3", "3:시장가,0:지정가(보통)"),
)]


def _momentum_market():
    row = {"rank_no": 1, "stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": 60_000,
           "flu_rt": Decimal(5), "now_trde_qty": 500_000, "sdnin_rt": Decimal(300)}
    return FakeMarket(ranks=[dict(row, sdnin_rt=None)], surges=[row])


def _momentum_buys(holdings: dict) -> list[str]:
    algo = MomentumScreen(meta={"code": "momentum_screen"}, params=ParamSet(MOM_DEFS, {}))
    ctx = make_ctx(FakeDb(), market=_momentum_market(), holdings=holdings)
    return [s.stk_cd for s in algo.evaluate(ctx) if s.side == "BUY"]


def test_missing_qty_does_not_cause_duplicate_momentum_buy():
    """종단: 보유 중인 종목에 수량 누락 WS 메시지가 와도 momentum_screen 이 재매수하지 않고,
    투입 한도 합계에도 남는다. (예전 동작: 행이 삭제돼 '미보유'로 보고 BUY 신호를 냈다.)"""
    db = HoldingTableDb()
    db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, trde_able_qty=10, pur_pric=60_000,
            cur_prc=60_000, pur_amt=600_000)
    svc(db).on_balance({"9001": "A005930", "302": "삼성전자", "10": "60000"})   # 930 없음

    holdings = {r["stk_cd"]: dict(r) for r in db.table.values()}
    assert "005930" in holdings
    assert _momentum_buys(holdings) == []                    # 이미 보유 → 신규 진입 없음
    ctx = make_ctx(FakeDb(), holdings=holdings)
    assert ctx.total_invested() == 600_000

    # 예전 동작(행 삭제) 재현: 같은 시세에서 중복 BUY 신호가 나왔다 - 테스트 픽스처 유효성 확인
    assert _momentum_buys({}) == ["005930"]


def test_real_zero_qty_allows_reentry_by_momentum():
    """반대 방향: 실제 전량 매도(0) 후에는 보유에서 빠져 정상적으로 신규 진입 대상이 된다."""
    db = HoldingTableDb()
    db.seed("005930", stk_nm="삼성전자", rmnd_qty=10, pur_pric=60_000, cur_prc=60_000,
            pur_amt=600_000)
    svc(db).on_balance({"9001": "A005930", "930": "0"})
    holdings = {r["stk_cd"]: dict(r) for r in db.table.values()}
    assert holdings == {}
    assert _momentum_buys(holdings) == ["005930"]


# ====================================================================== #
# REST kt00018 현재가 부호 (WS 경로와 동일하게 abs)
# ====================================================================== #
def _kt00018(cur_prc):
    return {"return_code": 0, "tot_pur_amt": "60000", "tot_evlt_amt": "61000",
            "tot_evlt_pl": "1000", "tot_prft_rt": "1.67", "prsm_dpst_aset_amt": "1000000",
            "acnt_evlt_remn_indv_tot": [
                {"stk_cd": "A005930", "stk_nm": "삼성전자", "rmnd_qty": "000000000001",
                 "trde_able_qty": "000000000001", "pur_pric": "000000060000",
                 "cur_prc": cur_prc, "pur_amt": "000000060000"}]}


def test_rest_cur_prc_is_absolute():
    for raw, want in (("-000000061000", 61_000), ("+000000061000", 61_000),
                      ("000000061000", 61_000), ("0", 0), ("", None)):
        rest = FakeRest({"kt00018": _kt00018(raw)})
        _, holdings = AccountService(FakeDb(), rest).fetch_evaluation()
        assert holdings[0]["cur_prc"] == want, raw
