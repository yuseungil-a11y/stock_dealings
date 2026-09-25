"""DB 접근 계층 (MariaDB / pymysql).

* 계정은 config.local.ini 의 `stock_svr` 만 사용한다(root 금지 - config 에서 검증).
* 스레드마다 별도 커넥션을 쓰고, 끊기면 자동 재연결한다.
* 컬럼명은 db/schema.sql 을 그대로 따른다.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
from decimal import Decimal
from typing import Any, Iterable, Sequence

import bcrypt
import pymysql
from pymysql.cursors import DictCursor

from .config import DbConfig
from .util import mask_text, now_kst

log = logging.getLogger(__name__)

STATUS_COMPONENTS = ("server", "db", "kiwoom_rest", "kiwoom_ws", "market")

# ---------------------------------------------------------------------- #
# 주문 게이트 3키 (R-10)
# ---------------------------------------------------------------------- #
# 이 키들은 일반 `set_setting()` 으로 바꿀 수 없다. 전용 `set_gate()` 만 허용하고,
# 호출자는 'UI 설정 탭의 REAL 확인을 통과했다'는 증거로 확인 토큰을 넘겨야 한다.
# (다른 모듈·스크립트가 실수로 실계좌 주문을 켜는 것을 막기 위한 장치)
GATE_KEYS = ("order_enabled", "real_trading_confirm", "trading_mode")

# 확인 토큰. UI 설정 탭의 확인 경로(ConfirmRealDialog 통과) 와 테스트만 사용한다.
GATE_CONFIRM_TOKEN = "stock_svr:gate-confirmed-by-ui"  # noqa: S105 - 비밀값 아님(오조작 방지용)

# 게이트를 '더 여는' 값 (event_log 레벨을 ERROR 로 올린다)
_GATE_OPENING_VALUES = {"order_enabled": "1", "real_trading_confirm": "1", "trading_mode": "real"}


# ---------------------------------------------------------------------- #
# 영구 보관 라우팅 (거래 성공/실패 사후 분석용)
# ---------------------------------------------------------------------- #
# `event_log`/`api_call_log` 는 7일 뒤 삭제되므로 실패 맥락이 사라진다.
# 아래 규칙에 걸리는 이벤트만 `event_archive` 에 한 벌 더 남겨 장기 보관한다.
ARCHIVE_LEVELS = ("WARN", "ERROR")
ARCHIVE_CATEGORIES = ("order", "algo", "engine", "risk")
# 반복 INFO 폭주 방지: 동기화·하트비트성 메시지는 보관 대상에서 뺀다
ARCHIVE_NOISE_WORDS = ("heartbeat", "하트비트", "동기화")
# 같은 (레벨, 분류, 메시지) 가 이 시간 안에 반복되면 1건만 보관한다
ARCHIVE_DEDUP_SEC = 60
# event_archive / api_error_log 보관기간 (system_setting.archive_retention_days)
ARCHIVE_RETENTION_DEFAULT = 365
ARCHIVE_RETENTION_MIN = 30

# `purge_old`/`purge_archives` 가 건드려도 되는 테이블 (그 외는 절대 삭제하지 않는다)
PURGEABLE_TABLES = ("event_log", "api_call_log", "screening_result",
                    "event_archive", "api_error_log")

# trend_scan_run 의 TEXT 컬럼(조사 요약)에 저장할 최대 길이
TREND_TEXT_MAX = 8000

# company_analysis_report.report_text(TEXT) 저장 상한
COMPANY_REPORT_TEXT_MAX = 16000


def _trim_masked(text: str | None, limit: int) -> str | None:
    """DB 저장 전 마스킹 + 길이 제한. 빈 값은 NULL."""
    if text is None:
        return None
    out = mask_text(str(text))[:limit].strip()
    return out or None


def should_archive_event(level: str, category: str, message: str) -> bool:
    """이 이벤트를 `event_archive` 에도 남길지 판단한다.

    * WARN/ERROR 는 분류와 무관하게 보관한다.
    * 그 외 레벨은 `order`/`algo`/`engine`/`risk` 분류만 보관하되,
      동기화·하트비트처럼 반복되는 INFO 는 제외한다.
    """
    lv = str(level or "").upper()
    if lv in ARCHIVE_LEVELS:
        return True
    if str(category or "").strip().lower() not in ARCHIVE_CATEGORIES:
        return False
    low = str(message or "").lower()
    return not any(word in low for word in ARCHIVE_NOISE_WORDS)


def should_log_api_error(http_status, return_code) -> bool:
    """이 API 호출을 `api_error_log`(영구 보관)에도 남길지 판단한다."""
    try:
        if http_status is not None and int(http_status) >= 400:
            return True
    except (TypeError, ValueError):
        pass
    if return_code is None:
        return False
    try:
        return int(return_code) != 0
    except (TypeError, ValueError):
        return True      # 해석 불가한 응답코드는 오류로 본다


# ---------------------------------------------------------------------- #
# 인증 (app_user, 웹과 공유) - 자동거래 시작 확인창 재검증용 (R-?? / auto_trade_dialog)
# ---------------------------------------------------------------------- #
# 계정이 없어도 동일한 연산비용을 들이기 위한 더미 해시(web/lib/auth.php 의
# AUTH_DUMMY_HASH 와 동일한 값 - 무작위 비밀번호의 bcrypt 해시이며 비밀값이 아니다).
_DUMMY_PASSWORD_HASH = "$2y$10$usesomesillystringfore7hnbRJHxXVLeakoG8K30oukPsA.ztMG"  # noqa: S105


def _check_password(password_hash: str | None, password: str) -> bool:
    """bcrypt 해시 대비 비밀번호를 검증하는 순수 함수(DB 접근 없음, 단위테스트용으로 분리).

    PHP `password_hash($x, PASSWORD_DEFAULT)` 가 만드는 해시는 `$2y$` 접두사를 쓰는데,
    Python `bcrypt` 라이브러리는 버전에 따라 이를 `$2b$` 로 바꿔줘야 검증되는 경우가
    있다(알고리즘은 동일, 버전 태그 표기만 다름) - 항상 치환한 뒤 검증한다.
    비밀번호 값은 반환값에도, 예외 메시지에도 절대 그대로 남기지 않는다.
    """
    if not password_hash:
        return False
    normalized = str(password_hash).replace("$2y$", "$2b$", 1)
    try:
        return bcrypt.checkpw(str(password).encode("utf-8"), normalized.encode("utf-8"))
    except (ValueError, TypeError):
        # 형식이 깨진 해시 등 - 실패로 처리한다(예외 메시지에 비밀번호가 담기지 않음).
        return False


class GateChangeError(PermissionError):
    """주문 게이트 3키를 허용되지 않은 경로로 바꾸려 할 때."""


class Database:
    """스레드 안전 커넥션 풀(스레드 로컬) + 도메인 헬퍼."""

    def __init__(self, cfg: DbConfig):
        self.cfg = cfg
        self._local = threading.local()
        self._all_conns: list[Any] = []
        self._conn_lock = threading.Lock()
        self.last_error: str | None = None
        # event_archive 중복 억제용 {(level, category, message): 마지막 기록시각}
        self._archive_seen: dict[tuple[str, str, str], _dt.datetime] = {}
        self._archive_lock = threading.Lock()

    # -- 커넥션 -------------------------------------------------------- #
    def _connect(self):
        return pymysql.connect(
            host=self.cfg.host,
            port=self.cfg.port,
            user=self.cfg.user,
            password=self.cfg.password,
            database=self.cfg.name,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
        )

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._connect()
            self._local.conn = c
            with self._conn_lock:
                self._all_conns.append(c)
            return c
        try:
            c.ping(reconnect=True)
        except Exception:  # noqa: BLE001 - 완전히 새로 연결
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
            c = self._connect()
            self._local.conn = c
            with self._conn_lock:
                self._all_conns.append(c)
        return c

    def close(self) -> None:
        with self._conn_lock:
            conns, self._all_conns = self._all_conns, []
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._local = threading.local()

    def ping(self) -> bool:
        try:
            self.query_one("SELECT 1 AS ok")
            self.last_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return False

    # -- 기본 질의 ----------------------------------------------------- #
    def query(self, sql: str, args: Sequence | None = None) -> list[dict]:
        with self.conn().cursor() as cur:
            cur.execute(sql, args or ())
            return list(cur.fetchall())

    def query_one(self, sql: str, args: Sequence | None = None) -> dict | None:
        with self.conn().cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchone()

    def scalar(self, sql: str, args: Sequence | None = None, default=None):
        row = self.query_one(sql, args)
        if not row:
            return default
        return next(iter(row.values()), default)

    def execute(self, sql: str, args: Sequence | None = None) -> int:
        with self.conn().cursor() as cur:
            return cur.execute(sql, args or ())

    def execute_many(self, sql: str, seq: Iterable[Sequence]) -> int:
        rows = list(seq)
        if not rows:
            return 0
        with self.conn().cursor() as cur:
            return cur.executemany(sql, rows)

    def insert(self, sql: str, args: Sequence | None = None) -> int:
        """INSERT 후 lastrowid 반환."""
        conn = self.conn()
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.lastrowid

    # ================================================================== #
    # system_setting
    # ================================================================== #
    def get_settings(self) -> dict[str, str]:
        return {r["setting_key"]: r["value"] for r in self.query("SELECT setting_key, value FROM system_setting")}

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM system_setting WHERE setting_key=%s", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str, updated_by: str = "server") -> None:
        """일반 설정 저장. **주문 게이트 3키는 거부**한다(R-10 - `set_gate` 사용)."""
        if key in GATE_KEYS:
            raise GateChangeError(
                f"'{key}' 는 주문 게이트 키입니다. Database.set_gate() 로만 변경할 수 있습니다.")
        self._write_setting(key, value, updated_by)

    def _write_setting(self, key: str, value: str, updated_by: str) -> None:
        self.execute(
            "INSERT INTO system_setting (setting_key, value, updated_by) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_by=VALUES(updated_by)",
            (key, str(value), updated_by),
        )

    def set_gate(self, key: str, value: str, *, confirm_token: str,
                 updated_by: str = "ui") -> bool:
        """주문 게이트 3키 전용 저장 (R-10).

        * `key` 는 GATE_KEYS 중 하나여야 한다.
        * `confirm_token` 은 UI 설정 탭이 REAL 확인 절차를 통과했다는 증거다.
        * 값이 실제로 바뀌면 `event_log` 에 기록한다(게이트를 더 여는 변경은 ERROR).

        반환: 값이 바뀌었으면 True.
        """
        if key not in GATE_KEYS:
            raise GateChangeError(f"'{key}' 는 주문 게이트 키가 아닙니다. set_setting() 을 쓰세요.")
        if confirm_token != GATE_CONFIRM_TOKEN:
            raise GateChangeError(f"게이트 변경 확인 토큰이 올바르지 않습니다 ({key})")
        new = str(value)
        old = self.get_setting(key)
        if old is not None and str(old) == new:
            return False
        self._write_setting(key, new, updated_by)
        level = "ERROR" if _GATE_OPENING_VALUES.get(key) == new else "WARN"
        try:
            self.log_event(level, "system",
                           f"주문 게이트 변경: {key} {old!r} → {new!r} (요청: {updated_by})")
        except Exception:  # noqa: BLE001 - 기록 실패가 저장을 되돌리지는 않는다
            log.warning("게이트 변경 event_log 기록 실패: %s", key, exc_info=True)
        return True

    # ================================================================== #
    # 계좌 / 잔고 / 보유
    # ================================================================== #
    def upsert_account(self, account_no: str, env: str, alias: str | None = None) -> int:
        self.execute(
            "INSERT INTO account (account_no, env, alias) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE alias=COALESCE(VALUES(alias), alias), is_active=1",
            (account_no, env, alias),
        )
        return int(self.scalar(
            "SELECT id FROM account WHERE account_no=%s AND env=%s", (account_no, env)))

    def get_account(self, account_id: int) -> dict | None:
        return self.query_one("SELECT * FROM account WHERE id=%s", (account_id,))

    def insert_balance(self, account_id: int, snapshot_at: _dt.datetime, data: dict) -> int:
        cols = ["entr", "d1_entra", "d2_entra", "ord_alow_amt", "pymn_alow_amt",
                "tot_pur_amt", "tot_evlt_amt", "tot_evlt_pl", "tot_prft_rt", "prsm_dpst_aset_amt"]
        sql = ("INSERT INTO account_balance (account_id, snapshot_at, " + ", ".join(cols) + ") "
               "VALUES (%s,%s," + ",".join(["%s"] * len(cols)) + ")")
        return self.insert(sql, [account_id, snapshot_at] + [data.get(c) for c in cols])

    def latest_balance(self, account_id: int) -> dict | None:
        return self.query_one(
            "SELECT * FROM account_balance WHERE account_id=%s ORDER BY snapshot_at DESC, id DESC LIMIT 1",
            (account_id,),
        )

    def replace_holdings(self, account_id: int, holdings: list[dict],
                         delete_missing: bool = True) -> int:
        """보유종목 전체 동기화.

        `delete_missing=False` 면 목록에 없는 종목을 지우지 않는다. 연속조회 상한(max_pages)
        때문에 일부 페이지만 받은 경우에 쓴다 (R-14 - 보유종목이 통째로 사라지는 사고 방지).
        """
        keep: list[str] = []
        sql = (
            "INSERT INTO holding (account_id, stk_cd, stk_nm, rmnd_qty, trde_able_qty, pur_pric, cur_prc, "
            "pur_amt, evlt_amt, evltv_prft, prft_rt, poss_rt) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE stk_nm=VALUES(stk_nm), rmnd_qty=VALUES(rmnd_qty), "
            "trde_able_qty=VALUES(trde_able_qty), pur_pric=VALUES(pur_pric), cur_prc=VALUES(cur_prc), "
            "pur_amt=VALUES(pur_amt), evlt_amt=VALUES(evlt_amt), evltv_prft=VALUES(evltv_prft), "
            "prft_rt=VALUES(prft_rt), poss_rt=VALUES(poss_rt)"
        )
        params = []
        for h in holdings:
            stk_cd = h.get("stk_cd")
            if not stk_cd:
                continue
            keep.append(stk_cd)
            params.append((
                account_id, stk_cd, h.get("stk_nm") or "", h.get("rmnd_qty") or 0, h.get("trde_able_qty"),
                h.get("pur_pric"), h.get("cur_prc"), h.get("pur_amt"), h.get("evlt_amt"),
                h.get("evltv_prft"), h.get("prft_rt"), h.get("poss_rt"),
            ))
        self.execute_many(sql, params)
        if not delete_missing:
            return len(params)
        if keep:
            ph = ",".join(["%s"] * len(keep))
            self.execute(f"DELETE FROM holding WHERE account_id=%s AND stk_cd NOT IN ({ph})",
                         [account_id] + keep)
        else:
            self.execute("DELETE FROM holding WHERE account_id=%s", (account_id,))
        return len(params)

    def get_holdings(self, account_id: int) -> list[dict]:
        return self.query("SELECT * FROM holding WHERE account_id=%s ORDER BY stk_cd", (account_id,))

    def snapshot_holdings(self, account_id: int, snap_date: _dt.date) -> int:
        return self.execute(
            "INSERT INTO holding_snapshot (account_id, snap_date, stk_cd, stk_nm, rmnd_qty, pur_pric, "
            "cur_prc, evlt_amt, evltv_prft, prft_rt) "
            "SELECT account_id, %s, stk_cd, stk_nm, rmnd_qty, pur_pric, cur_prc, evlt_amt, evltv_prft, prft_rt "
            "FROM holding WHERE account_id=%s "
            "ON DUPLICATE KEY UPDATE rmnd_qty=VALUES(rmnd_qty), pur_pric=VALUES(pur_pric), "
            "cur_prc=VALUES(cur_prc), evlt_amt=VALUES(evlt_amt), evltv_prft=VALUES(evltv_prft), "
            "prft_rt=VALUES(prft_rt)",
            (snap_date, account_id),
        )

    # -- position_state ------------------------------------------------ #
    def get_position_state(self, account_id: int, stk_cd: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM position_state WHERE account_id=%s AND stk_cd=%s", (account_id, stk_cd))

    def get_position_states(self, account_id: int) -> dict[str, dict]:
        return {r["stk_cd"]: r for r in
                self.query("SELECT * FROM position_state WHERE account_id=%s", (account_id,))}

    def upsert_position_state(self, account_id: int, stk_cd: str, **fields) -> None:
        allowed = ("entry_algo", "first_buy_at", "last_buy_price", "avg_down_count", "total_invested", "stopped")
        sets = {k: v for k, v in fields.items() if k in allowed}
        cols = ["account_id", "stk_cd"] + list(sets)
        vals = [account_id, stk_cd] + list(sets.values())
        upd = ", ".join(f"{k}=VALUES({k})" for k in sets) or "updated_at=CURRENT_TIMESTAMP"
        self.execute(
            f"INSERT INTO position_state ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON DUPLICATE KEY UPDATE {upd}",
            vals,
        )

    def reset_stale_positions(self, account_id: int, held_codes: list[str], today: _dt.date) -> int:
        """보유목록에 없는 종목의 투입금/물타기 회차를 초기화한다 (B4/S-13).

        `stopped`(손절 후 재진입 금지)는 **당일에만** 유지하고 익일 자동 해제한다.
        """
        n = 0
        if held_codes:
            ph = ",".join(["%s"] * len(held_codes))
            n = self.execute(
                f"UPDATE position_state SET total_invested=0, avg_down_count=0, last_buy_price=NULL "
                f"WHERE account_id=%s AND stk_cd NOT IN ({ph}) "
                f"AND (total_invested<>0 OR avg_down_count<>0)",
                [account_id] + list(held_codes))
        else:
            n = self.execute(
                "UPDATE position_state SET total_invested=0, avg_down_count=0, last_buy_price=NULL "
                "WHERE account_id=%s AND (total_invested<>0 OR avg_down_count<>0)", (account_id,))
        # 손절 표시는 하루가 지나면 해제
        self.execute(
            "UPDATE position_state SET stopped=0 "
            "WHERE account_id=%s AND stopped=1 AND DATE(updated_at) < %s", (account_id, today))
        return n

    def reduce_position_invest(self, account_id: int, stk_cd: str, amount: int) -> None:
        """매도 체결분만큼 누적 투입금을 차감한다(0 미만으로 내려가지 않음) (B4)."""
        self.execute(
            "UPDATE position_state SET total_invested=GREATEST(0, total_invested-%s) "
            "WHERE account_id=%s AND stk_cd=%s", (max(0, int(amount)), account_id, stk_cd))

    def bump_position_invest(self, account_id: int, stk_cd: str, amount: int,
                             algo_code: str | None, price: int | None, avg_down: bool) -> None:
        self.execute(
            "INSERT INTO position_state (account_id, stk_cd, entry_algo, first_buy_at, last_buy_price, "
            "avg_down_count, total_invested) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE last_buy_price=VALUES(last_buy_price), "
            "total_invested=total_invested+VALUES(total_invested), "
            "avg_down_count=avg_down_count+%s, entry_algo=COALESCE(entry_algo, VALUES(entry_algo))",
            (account_id, stk_cd, algo_code, now_kst(), price, 1 if avg_down else 0, int(amount),
             1 if avg_down else 0),
        )

    # ================================================================== #
    # 종목 / 시세
    # ================================================================== #
    def upsert_stock_master(self, rows: list[dict]) -> int:
        sql = (
            "INSERT INTO stock_master (stk_cd, stk_nm, market_code, market_name, up_name, list_count, "
            "last_price, state, order_warning, nxt_enable, reg_day) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE stk_nm=VALUES(stk_nm), market_code=VALUES(market_code), "
            "market_name=VALUES(market_name), up_name=VALUES(up_name), list_count=VALUES(list_count), "
            "last_price=VALUES(last_price), state=VALUES(state), order_warning=VALUES(order_warning), "
            "nxt_enable=VALUES(nxt_enable), reg_day=VALUES(reg_day)"
        )
        params = [
            (r.get("stk_cd"), (r.get("stk_nm") or "")[:60], r.get("market_code"), r.get("market_name"),
             r.get("up_name"), r.get("list_count"), r.get("last_price"), (r.get("state") or None),
             r.get("order_warning"), r.get("nxt_enable"), r.get("reg_day"))
            for r in rows if r.get("stk_cd")
        ]
        return self.execute_many(sql, params)

    def stock_name(self, stk_cd: str) -> str | None:
        return self.scalar("SELECT stk_nm FROM stock_master WHERE stk_cd=%s", (stk_cd,))

    def stock_state(self, stk_cd: str) -> str | None:
        """stock_master.state (거래정지/정리매매/상장폐지 등 표기). 없으면 None."""
        return self.scalar("SELECT state FROM stock_master WHERE stk_cd=%s", (stk_cd,))

    def stock_master_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM stock_master", default=0) or 0)

    def stock_master_updated_today(self, today: _dt.date) -> bool:
        cnt = self.scalar("SELECT COUNT(*) FROM stock_master WHERE DATE(updated_at)=%s", (today,), default=0)
        return bool(cnt)

    def stock_master_stats(self) -> dict:
        """종목마스터 건수와 최신 갱신 시각 (universe_filter 순위 캐시 무효화 판단용)."""
        row = self.query_one("SELECT COUNT(*) AS cnt, MAX(updated_at) AS updated_at FROM stock_master")
        row = row or {}
        return {"count": int(row.get("cnt") or 0), "updated_at": row.get("updated_at")}

    def stock_master_universe(self, market_codes: Sequence[str] = ("0", "10")) -> list[dict]:
        """시가총액 순위 계산에 쓰는 종목마스터 행(코스피·코스닥). 읽기 전용."""
        codes = [str(c) for c in market_codes] or ["0", "10"]
        holes = ",".join(["%s"] * len(codes))
        return self.query(
            "SELECT stk_cd, stk_nm, market_code, market_name, list_count, last_price, "
            f"state, order_warning, updated_at FROM stock_master WHERE market_code IN ({holes})",
            tuple(codes),
        )

    def upsert_price_daily(self, stk_cd: str, bars: list[dict]) -> int:
        sql = (
            "INSERT INTO price_daily (stk_cd, dt, open_pric, high_pric, low_pric, cur_prc, trde_qty, trde_prica) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE open_pric=VALUES(open_pric), high_pric=VALUES(high_pric), "
            "low_pric=VALUES(low_pric), cur_prc=VALUES(cur_prc), trde_qty=VALUES(trde_qty), "
            "trde_prica=VALUES(trde_prica)"
        )
        params = [
            (stk_cd, b["dt"], b.get("open_pric"), b.get("high_pric"), b.get("low_pric"),
             b.get("cur_prc"), b.get("trde_qty"), b.get("trde_prica"))
            for b in bars if b.get("dt")
        ]
        return self.execute_many(sql, params)

    def recent_bars(self, stk_cd: str, limit: int = 30) -> list[dict]:
        """최신순 → 과거순으로 정렬해서 반환."""
        rows = self.query(
            "SELECT * FROM price_daily WHERE stk_cd=%s ORDER BY dt DESC LIMIT %s", (stk_cd, int(limit)))
        return list(reversed(rows))

    def insert_screening(self, captured_at: _dt.datetime, source_api: str, rows: list[dict]) -> int:
        sql = (
            "INSERT INTO screening_result (captured_at, source_api, rank_no, stk_cd, stk_nm, cur_prc, "
            "flu_rt, now_trde_qty, sdnin_rt) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        params = [
            (captured_at, source_api, r.get("rank_no"), r.get("stk_cd"), (r.get("stk_nm") or "")[:60],
             r.get("cur_prc"), r.get("flu_rt"), r.get("now_trde_qty"), r.get("sdnin_rt"))
            for r in rows if r.get("stk_cd")
        ]
        return self.execute_many(sql, params)

    # ================================================================== #
    # 알고리즘 / 파라미터
    # ================================================================== #
    def load_algorithms(self) -> list[dict]:
        algos = self.query(
            "SELECT a.*, COALESCE(s.is_enabled,0) AS is_enabled, COALESCE(s.priority, a.sort_order) AS priority "
            "FROM algorithm a LEFT JOIN algorithm_selection s ON s.algorithm_id=a.id "
            "ORDER BY COALESCE(s.priority, a.sort_order), a.id"
        )
        defs = self.query("SELECT * FROM algorithm_param_def ORDER BY algorithm_id, sort_order, id")
        vals = {(r["algorithm_id"], r["param_key"]): r["value"]
                for r in self.query("SELECT * FROM algorithm_param_value")}
        by_algo: dict[int, list[dict]] = {}
        for d in defs:
            d = dict(d)
            d["value"] = vals.get((d["algorithm_id"], d["param_key"]), d["default_value"])
            by_algo.setdefault(d["algorithm_id"], []).append(d)
        for a in algos:
            a["param_defs"] = by_algo.get(a["id"], [])
            a["params"] = {d["param_key"]: d["value"] for d in a["param_defs"]}
        return algos

    def save_param(self, algorithm_id: int, param_key: str, value: str, updated_by: str = "ui") -> bool:
        old = self.scalar(
            "SELECT value FROM algorithm_param_value WHERE algorithm_id=%s AND param_key=%s",
            (algorithm_id, param_key))
        if old is not None and str(old) == str(value):
            return False
        self.execute(
            "INSERT INTO algorithm_param_value (algorithm_id, param_key, value, updated_by) VALUES (%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE value=VALUES(value), updated_by=VALUES(updated_by)",
            (algorithm_id, param_key, str(value), updated_by),
        )
        self.execute(
            "INSERT INTO algorithm_param_history (algorithm_id, param_key, old_value, new_value, changed_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (algorithm_id, param_key, old, str(value), updated_by),
        )
        return True

    def save_selection(self, algorithm_id: int, is_enabled: bool, priority: int, updated_by: str = "ui") -> None:
        self.execute(
            "INSERT INTO algorithm_selection (algorithm_id, is_enabled, priority, updated_by) "
            "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE is_enabled=VALUES(is_enabled), "
            "priority=VALUES(priority), updated_by=VALUES(updated_by)",
            (algorithm_id, 1 if is_enabled else 0, int(priority), updated_by),
        )

    def insert_signal(self, run_id: int | None, algo_code: str, stk_cd: str, stk_nm: str | None,
                      signal_type: str, score=None, detail: str | None = None,
                      order_id: int | None = None) -> int:
        return self.insert(
            "INSERT INTO signal_log (run_id, algo_code, stk_cd, stk_nm, signal_type, score, detail, order_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (run_id, algo_code, stk_cd, (stk_nm or None), signal_type,
             score if score is None else Decimal(str(score)), (detail or "")[:500] or None, order_id),
        )

    # ================================================================== #
    # Claude 거부권 필터 판단 기록 (llm_decision_log)
    # ================================================================== #
    _LLM_COLS = ("run_id", "stk_cd", "stk_nm", "source_algo", "side", "model", "decision",
                 "final_action", "confidence", "reasons", "risk_flags", "input_summary",
                 "from_cache", "latency_ms", "input_tokens", "output_tokens", "error_msg",
                 "order_id")

    def insert_llm_decision(self, **f) -> int:
        """Claude 검토 1건 기록. 입력 요약에도 계좌·키 정보는 들어가지 않는다."""
        vals = []
        for c in self._LLM_COLS:
            v = f.get(c)
            if isinstance(v, str) and c in ("reasons", "risk_flags", "error_msg", "stk_nm",
                                            "source_algo", "model"):
                v = mask_text(v)
            vals.append(v)
        sql = (f"INSERT INTO llm_decision_log ({', '.join(self._LLM_COLS)}) "
               f"VALUES ({', '.join(['%s'] * len(self._LLM_COLS))})")
        return self.insert(sql, vals)

    def find_stocks_by_name(self, stk_nm: str) -> list[dict]:
        """종목명이 **정확히 일치**하는 코스피/코스닥 종목(공백만 무시).

        Claude 가 알려준 회사명을 종목코드로 바꾸는 유일한 경로다(추측 매칭 금지).
        """
        name = "".join(str(stk_nm or "").split())
        if not name:
            return []
        return self.query(
            "SELECT stk_cd, stk_nm, market_code, market_name, last_price, state, order_warning "
            "FROM stock_master WHERE market_code IN ('0','10') "
            "AND REPLACE(REPLACE(stk_nm, ' ', ''), '\t', '') = %s",
            (name,))

    # ================================================================== #
    # 산업 트렌드 스캔 (claude_trend_scan)
    # ================================================================== #
    def trend_scan_run_on(self, scan_date: _dt.date) -> dict | None:
        return self.query_one("SELECT * FROM trend_scan_run WHERE scan_date=%s", (scan_date,))

    def start_trend_scan_run(self, scan_date: _dt.date, region_scope: str, model: str) -> int:
        """오늘자 스캔 행을 만든다. 같은 날짜가 이미 있으면 UNIQUE 위반으로 예외가 난다
        (프로세스가 재기동돼도 같은 날 두 번 조사하지 않게 하는 이중 방지선)."""
        return self.insert(
            "INSERT INTO trend_scan_run (scan_date, started_at, status, region_scope, model) "
            "VALUES (%s,%s,'ok',%s,%s)",
            (scan_date, now_kst(), str(region_scope)[:20], str(model)[:50]))

    def finish_trend_scan_run(self, run_id: int, *, status: str = "ok",
                              candidate_count: int = 0, signal_count: int = 0,
                              domestic_theme_summary: str | None = None,
                              research_summary: str | None = None,
                              web_search_count: int | None = None,
                              input_tokens: int | None = None,
                              output_tokens: int | None = None,
                              latency_ms: int | None = None,
                              error_msg: str | None = None) -> int:
        """스캔 결과 기록. 요약/오류 문구에는 마스킹을 한 번 더 적용한다(비밀값 방지)."""
        return self.execute(
            "UPDATE trend_scan_run SET finished_at=%s, status=%s, domestic_theme_summary=%s, "
            "research_summary=%s, candidate_count=%s, signal_count=%s, web_search_count=%s, "
            "input_tokens=%s, output_tokens=%s, latency_ms=%s, error_msg=%s WHERE id=%s",
            (now_kst(), status if status in ("ok", "partial", "error") else "error",
             _trim_masked(domestic_theme_summary, TREND_TEXT_MAX),
             _trim_masked(research_summary, TREND_TEXT_MAX),
             int(candidate_count), int(signal_count), web_search_count,
             input_tokens, output_tokens, latency_ms,
             _trim_masked(error_msg, 255), int(run_id)))

    def insert_trend_candidate(self, run_id: int, *, region: str, theme: str,
                               rationale: str | None = None, confidence: int | None = None,
                               kiwoom_theme_cd: str | None = None,
                               kiwoom_theme_nm: str | None = None,
                               stk_cd: str | None = None, stk_nm: str | None = None,
                               match_status: str = "unmatched",
                               signal_id: int | None = None) -> int:
        return self.insert(
            "INSERT INTO trend_scan_candidate (run_id, region, theme, rationale, confidence, "
            "kiwoom_theme_cd, kiwoom_theme_nm, stk_cd, stk_nm, match_status, signal_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (int(run_id), region if region in ("domestic", "global") else "domestic",
             _trim_masked(theme, 120) or "-", _trim_masked(rationale, 500),
             None if confidence is None else int(confidence),
             (kiwoom_theme_cd or None), _trim_masked(kiwoom_theme_nm, 60),
             (stk_cd or None), _trim_masked(stk_nm, 60), match_status, signal_id))

    def set_trend_candidate_signal(self, candidate_id: int, signal_id: int) -> int:
        return self.execute("UPDATE trend_scan_candidate SET signal_id=%s WHERE id=%s",
                            (int(signal_id), int(candidate_id)))

    def trend_scan_candidates(self, run_id: int) -> list[dict]:
        return self.query("SELECT * FROM trend_scan_candidate WHERE run_id=%s ORDER BY id",
                          (int(run_id),))

    # -- 시도 감사로그 (append-only) ------------------------------------ #
    def insert_trend_scan_attempt(self, scan_date: _dt.date, *, trigger_type: str,
                                  status: str, requested_by: str | None = None,
                                  region_scope: str | None = None, model: str | None = None,
                                  candidate_count: int | None = None,
                                  web_search_count: int | None = None,
                                  input_tokens: int | None = None,
                                  output_tokens: int | None = None,
                                  latency_ms: int | None = None,
                                  error_msg: str | None = None,
                                  research_summary: str | None = None,
                                  started_at: _dt.datetime | None = None,
                                  finished_at: _dt.datetime | None = None) -> int:
        """시도 1건을 **새 행으로 추가**한다. 기존 행은 절대 갱신·삭제하지 않는다.

        `trend_scan_run` 은 하루 1행(최신 공식 결과)만 남지만, 이 테이블에는 실패한
        시도까지 전부 남아 "그때 무슨 일이 있었는지"를 나중에 되짚을 수 있다.
        """
        return self.insert(
            "INSERT INTO trend_scan_attempt (scan_date, trigger_type, requested_by, status, "
            "region_scope, model, candidate_count, web_search_count, input_tokens, "
            "output_tokens, latency_ms, error_msg, research_summary, started_at, finished_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (scan_date,
             trigger_type if trigger_type in ("scheduled", "manual") else "scheduled",
             _trim_masked(requested_by, 50),
             status if status in ("ok", "partial", "error") else "error",
             (str(region_scope)[:20] if region_scope else None),
             (str(model)[:50] if model else None),
             candidate_count, web_search_count, input_tokens, output_tokens, latency_ms,
             _trim_masked(error_msg, 255), _trim_masked(research_summary, TREND_TEXT_MAX),
             started_at or now_kst(), finished_at or now_kst()))

    def trend_scan_attempts(self, scan_date: _dt.date) -> list[dict]:
        return self.query(
            "SELECT * FROM trend_scan_attempt WHERE scan_date=%s ORDER BY id", (scan_date,))

    # -- 수동 재조사 결과 반영 (run UPSERT + 후보 교체, 한 트랜잭션) ----- #
    def save_manual_trend_scan(self, scan_date: _dt.date, *, requested_by: str | None,
                               status: str, region_scope: str, model: str,
                               candidates: list[dict],
                               domestic_theme_summary: str | None = None,
                               research_summary: str | None = None,
                               web_search_count: int | None = None,
                               input_tokens: int | None = None,
                               output_tokens: int | None = None,
                               latency_ms: int | None = None,
                               error_msg: str | None = None,
                               started_at: _dt.datetime | None = None) -> int:
        """수동 재조사 결과로 **오늘 행을 덮어쓰고** 후보를 통째로 교체한다.

        `trend_scan_run` 은 `UNIQUE(scan_date)` 이므로 오늘 행이 있으면 갱신된다.
        후보 삭제 → 재삽입을 한 트랜잭션으로 묶어, 웹 조회가 '후보 0건' 인 중간 상태를
        보지 않게 한다. `signal_count` 는 **항상 0**(관찰 전용 경로라 주문·신호가 없다).
        """
        cand_sql = ("INSERT INTO trend_scan_candidate (run_id, region, theme, rationale, "
                    "confidence, kiwoom_theme_cd, kiwoom_theme_nm, stk_cd, stk_nm, "
                    "match_status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
        conn = self.conn()
        conn.begin()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO trend_scan_run (scan_date, trigger_type, requested_by, "
                    "started_at, finished_at, status, region_scope, model, "
                    "domestic_theme_summary, research_summary, candidate_count, signal_count, "
                    "web_search_count, input_tokens, output_tokens, latency_ms, error_msg) "
                    "VALUES (%s,'manual',%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE trigger_type='manual', "
                    "requested_by=VALUES(requested_by), started_at=VALUES(started_at), "
                    "finished_at=VALUES(finished_at), status=VALUES(status), "
                    "region_scope=VALUES(region_scope), model=VALUES(model), "
                    "domestic_theme_summary=VALUES(domestic_theme_summary), "
                    "research_summary=VALUES(research_summary), "
                    "candidate_count=VALUES(candidate_count), signal_count=0, "
                    "web_search_count=VALUES(web_search_count), "
                    "input_tokens=VALUES(input_tokens), output_tokens=VALUES(output_tokens), "
                    "latency_ms=VALUES(latency_ms), error_msg=VALUES(error_msg)",
                    (scan_date, _trim_masked(requested_by, 50), started_at or now_kst(),
                     now_kst(), status if status in ("ok", "partial", "error") else "error",
                     str(region_scope)[:20], str(model)[:50],
                     _trim_masked(domestic_theme_summary, TREND_TEXT_MAX),
                     _trim_masked(research_summary, TREND_TEXT_MAX),
                     int(len(candidates)), web_search_count, input_tokens, output_tokens,
                     latency_ms, _trim_masked(error_msg, 255)))
                cur.execute("SELECT id FROM trend_scan_run WHERE scan_date=%s", (scan_date,))
                run_id = int((cur.fetchone() or {}).get("id"))
                cur.execute("DELETE FROM trend_scan_candidate WHERE run_id=%s", (run_id,))
                rows = [(run_id,
                         c.get("region") if c.get("region") in ("domestic", "global")
                         else "domestic",
                         _trim_masked(c.get("theme"), 120) or "-",
                         _trim_masked(c.get("rationale"), 500),
                         None if c.get("confidence") is None else int(c["confidence"]),
                         c.get("kiwoom_theme_cd") or None,
                         _trim_masked(c.get("kiwoom_theme_nm"), 60),
                         c.get("stk_cd") or None, _trim_masked(c.get("stk_nm"), 60),
                         c.get("match_status") or "unmatched") for c in candidates]
                if rows:
                    cur.executemany(cand_sql, rows)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        return run_id

    # -- 수동 재조사 요청 큐 (웹이 INSERT, 서버만 갱신) ------------------ #
    def pending_trend_scan_request(self) -> dict | None:
        """가장 오래된 pending 요청 1건(claim 전 조회)."""
        return self.query_one(
            "SELECT * FROM trend_scan_request WHERE status='pending' "
            "ORDER BY requested_at, id LIMIT 1")

    def count_processing_trend_requests(self) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) AS n FROM trend_scan_request WHERE status='processing'",
            default=0) or 0)

    def claim_trend_scan_request(self, max_tries: int = 5) -> dict | None:
        """pending 요청 1건을 **원자적으로** claim 한다. 못 잡으면 None.

        조회와 갱신 사이에 다른 프로세스가 먼저 집을 수 있으므로,
        `UPDATE ... WHERE id=%s AND status='pending'` 의 **영향 행 수**로 승자를 가린다
        (0이면 남이 가져간 것 → 다음 건으로).
        """
        for _ in range(max(1, int(max_tries))):
            row = self.pending_trend_scan_request()
            if row is None:
                return None
            won = self.execute(
                "UPDATE trend_scan_request SET status='processing' "
                "WHERE id=%s AND status='pending'", (int(row["id"]),))
            if won:
                out = dict(row)
                out["status"] = "processing"
                return out
        return None

    def finish_trend_scan_request(self, request_id: int, *, status: str,
                                  run_id: int | None = None,
                                  error_msg: str | None = None) -> int:
        return self.execute(
            "UPDATE trend_scan_request SET status=%s, run_id=%s, error_msg=%s, processed_at=%s "
            "WHERE id=%s",
            (status if status in ("pending", "processing", "done", "error") else "error",
             None if run_id is None else int(run_id), _trim_masked(error_msg, 255),
             now_kst(), int(request_id)))

    def expire_stale_trend_requests(self, minutes: int = 10,
                                    reason: str = "서버 재시작으로 중단") -> int:
        """`processing` 인 채 오래 멈춰 있는 요청을 `error` 로 정리한다.

        처리 도중 서버가 죽으면 그 행이 영원히 `processing` 으로 남아 다음 요청이
        영영 처리되지 않는다(동시 1건 제한). 그 교착을 푸는 방어 코드다.
        """
        return self.execute(
            "UPDATE trend_scan_request SET status='error', error_msg=%s, processed_at=%s "
            "WHERE status='processing' AND requested_at < (NOW() - INTERVAL %s MINUTE)",
            (_trim_masked(reason, 255), now_kst(), max(1, int(minutes))))

    def llm_usage_today(self) -> dict[str, int]:
        """당일 Claude 실제 호출 수와 토큰 사용량(캐시 적중분 제외)."""
        row = self.query_one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(input_tokens),0) AS in_tok, "
            "COALESCE(SUM(output_tokens),0) AS out_tok FROM llm_decision_log "
            "WHERE from_cache=0 AND DATE(created_at)=CURDATE()")
        if not row:
            return {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        return {"calls": int(row.get("calls") or 0),
                "input_tokens": int(row.get("in_tok") or 0),
                "output_tokens": int(row.get("out_tok") or 0)}

    # ================================================================== #
    # 기업 재무분석 (DART + Claude) — **매매와 무관한 읽기 전용 참고 리포트**
    #
    # 이 구역의 어떤 메서드도 algorithm / algorithm_selection / signal_log / orders /
    # system_setting 게이트 3키를 읽거나 쓰지 않는다.
    # ================================================================== #
    def company_corp_code_stats(self) -> dict:
        """매핑 건수와 최신 갱신 시각(월 1회 갱신 판단용)."""
        row = self.query_one(
            "SELECT COUNT(*) AS cnt, MAX(updated_at) AS updated_at FROM company_corp_code") or {}
        return {"count": int(row.get("cnt") or 0), "updated_at": row.get("updated_at")}

    def upsert_company_corp_codes(self, rows: list[dict]) -> int:
        sql = ("INSERT INTO company_corp_code (stk_cd, corp_code, corp_name) VALUES (%s,%s,%s) "
               "ON DUPLICATE KEY UPDATE corp_code=VALUES(corp_code), corp_name=VALUES(corp_name)")
        params = [(str(r["stk_cd"])[:12], str(r["corp_code"])[:8],
                   _trim_masked(r.get("corp_name"), 120) or str(r["stk_cd"]))
                  for r in rows if r.get("stk_cd") and r.get("corp_code")]
        return self.execute_many(sql, params)

    def company_corp_codes(self, stk_cds: Sequence[str] | None = None) -> list[dict]:
        if stk_cds is None:
            return self.query("SELECT * FROM company_corp_code ORDER BY stk_cd")
        codes = [str(c) for c in stk_cds if c]
        if not codes:
            return []
        holes = ",".join(["%s"] * len(codes))
        return self.query(
            f"SELECT * FROM company_corp_code WHERE stk_cd IN ({holes}) ORDER BY stk_cd",
            tuple(codes))

    def company_financial_keys(self, stk_cds: Sequence[str]) -> set[tuple[str, int, str]]:
        """이미 저장된 (종목, 연도, 보고서코드) 조합. 재조회를 건너뛰기 위한 캐시 키."""
        codes = [str(c) for c in stk_cds if c]
        if not codes:
            return set()
        holes = ",".join(["%s"] * len(codes))
        rows = self.query(
            "SELECT stk_cd, bsns_year, reprt_code FROM company_financial "
            f"WHERE stk_cd IN ({holes})", tuple(codes))
        return {(str(r["stk_cd"]), int(r["bsns_year"]), str(r["reprt_code"])) for r in rows}

    _FINANCIAL_COLS = ("revenue", "operating_profit", "net_profit", "total_assets",
                       "total_liabilities", "total_equity", "eps", "operating_cash_flow",
                       "shares_outstanding")

    def upsert_company_financial(self, stk_cd: str, bsns_year: int, reprt_code: str,
                                 **values) -> int:
        cols = ("stk_cd", "bsns_year", "reprt_code", *self._FINANCIAL_COLS, "fetched_at")
        updates = ", ".join(f"{c}=VALUES({c})" for c in (*self._FINANCIAL_COLS, "fetched_at"))
        args = [str(stk_cd)[:12], int(bsns_year), str(reprt_code)[:5]]
        for col in self._FINANCIAL_COLS:
            value = values.get(col)
            args.append(None if value is None else int(value))
        args.append(now_kst())
        return self.execute(
            f"INSERT INTO company_financial ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) ON DUPLICATE KEY UPDATE {updates}",
            args)

    def company_financials(self, stk_cd: str, since_year: int | None = None) -> list[dict]:
        """한 종목의 재무제표 행(과거 → 최신). `since_year` 이상만."""
        if since_year is None:
            return self.query(
                "SELECT * FROM company_financial WHERE stk_cd=%s "
                "ORDER BY bsns_year, reprt_code", (str(stk_cd),))
        return self.query(
            "SELECT * FROM company_financial WHERE stk_cd=%s AND bsns_year>=%s "
            "ORDER BY bsns_year, reprt_code", (str(stk_cd), int(since_year)))

    def upsert_company_valuation(self, stk_cd: str, dt: _dt.date, **values) -> int:
        cols = ("stk_cd", "dt", "cur_prc", "eps_ttm", "bps", "per", "pbr", "roe",
                "debt_ratio", "financial_asof")
        updates = ", ".join(f"{c}=VALUES({c})" for c in cols[2:])
        args = [str(stk_cd)[:12], dt]
        for col in cols[2:]:
            value = values.get(col)
            if col == "financial_asof":
                args.append(str(value)[:20] if value else None)
            elif col in ("per", "pbr", "roe", "debt_ratio"):
                args.append(None if value is None else Decimal(str(value)))
            else:
                args.append(None if value is None else int(value))
        return self.execute(
            f"INSERT INTO company_valuation_daily ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) ON DUPLICATE KEY UPDATE {updates}",
            args)

    def company_valuation(self, stk_cd: str, dt: _dt.date) -> dict | None:
        return self.query_one(
            "SELECT * FROM company_valuation_daily WHERE stk_cd=%s AND dt=%s",
            (str(stk_cd), dt))

    def latest_company_valuation(self, stk_cd: str) -> dict | None:
        """이 종목의 가장 최근 평가일 행(없으면 None). `fundamentals_filter` 가 쓴다."""
        return self.query_one(
            "SELECT * FROM company_valuation_daily WHERE stk_cd=%s "
            "ORDER BY dt DESC LIMIT 1",
            (str(stk_cd),))

    def upsert_company_report(self, stk_cd: str, as_of_date: _dt.date, *, model: str,
                              report_text: str, stk_nm: str | None = None,
                              summary: str | None = None, input_tokens: int | None = None,
                              output_tokens: int | None = None, latency_ms: int | None = None,
                              status: str = "ok", error_msg: str | None = None) -> int:
        """같은 날 재실행하면 **갱신**한다(UNIQUE(stk_cd, as_of_date))."""
        return self.execute(
            "INSERT INTO company_analysis_report (stk_cd, stk_nm, as_of_date, model, summary, "
            "report_text, input_tokens, output_tokens, latency_ms, status, error_msg) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE stk_nm=VALUES(stk_nm), model=VALUES(model), "
            "summary=VALUES(summary), report_text=VALUES(report_text), "
            "input_tokens=VALUES(input_tokens), output_tokens=VALUES(output_tokens), "
            "latency_ms=VALUES(latency_ms), status=VALUES(status), "
            "error_msg=VALUES(error_msg), created_at=VALUES(created_at)",
            (str(stk_cd)[:12], _trim_masked(stk_nm, 60), as_of_date, str(model)[:50],
             _trim_masked(summary, 500), _trim_masked(report_text, COMPANY_REPORT_TEXT_MAX) or "-",
             input_tokens, output_tokens, latency_ms,
             status if status in ("ok", "error") else "error", _trim_masked(error_msg, 255)))

    def company_report_on(self, stk_cd: str, as_of_date: _dt.date) -> dict | None:
        return self.query_one(
            "SELECT * FROM company_analysis_report WHERE stk_cd=%s AND as_of_date=%s",
            (str(stk_cd), as_of_date))

    def company_reported_codes(self, as_of_date: _dt.date) -> set[str]:
        """그날 이미 리포트를 만든 종목(성공분만). 하루 상한 안에서 다음 종목으로 넘어가기 위해."""
        rows = self.query(
            "SELECT stk_cd FROM company_analysis_report WHERE as_of_date=%s AND status='ok'",
            (as_of_date,))
        return {str(r["stk_cd"]) for r in rows}

    def latest_closes(self, stk_cds: Sequence[str]) -> dict[str, int]:
        """`price_daily` 의 최신 종가(있는 종목만). 없으면 호출부가 stock_master 로 대체한다."""
        codes = [str(c) for c in stk_cds if c]
        if not codes:
            return {}
        holes = ",".join(["%s"] * len(codes))
        rows = self.query(
            "SELECT p.stk_cd, p.cur_prc FROM price_daily p JOIN ("
            f"  SELECT stk_cd, MAX(dt) AS dt FROM price_daily WHERE stk_cd IN ({holes}) "
            "  GROUP BY stk_cd) m ON m.stk_cd=p.stk_cd AND m.dt=p.dt",
            tuple(codes))
        return {str(r["stk_cd"]): int(r["cur_prc"]) for r in rows if r.get("cur_prc")}

    def stock_master_row(self, stk_cd: str) -> dict | None:
        """종목마스터 단건(상장주식수·전일종가 등). 온디맨드 재무데이터 수집 대상 조회용."""
        return self.query_one(
            "SELECT stk_cd, stk_nm, market_code, market_name, list_count, last_price, "
            "state, order_warning FROM stock_master WHERE stk_cd=%s", (str(stk_cd),))

    # ================================================================== #
    # 온디맨드 재무데이터 수집 요청 큐 (fundamentals_filter 전용)
    #
    # fundamentals_filter(매매 사이클 안의 순수 DB 읽기 필터)가 "재무데이터 없음/오래됨"
    # 으로 매수를 차단할 때, 그 종목만 재수집하도록 남기는 요청이다. 이 메서드들은
    # DART 를 절대 호출하지 않는다(빠른 INSERT/UPDATE 뿐) - 실제 수집은
    # `services.fundamentals_fetch.FetchRequestWorker` 가 폴링해서 한다.
    # ================================================================== #
    def open_fetch_request(self, stk_cd: str) -> dict | None:
        """이미 pending/processing 인 요청(중복 요청 방지용 dedupe 조회)."""
        return self.query_one(
            "SELECT * FROM company_fetch_request WHERE stk_cd=%s "
            "AND status IN ('pending','processing') ORDER BY id DESC LIMIT 1",
            (str(stk_cd),))

    def recent_fetch_request(self, stk_cd: str, minutes: int = 60) -> dict | None:
        """최근 `minutes` 분 안에 끝난(성공/실패 불문) 요청(재요청 쿨다운 판단용)."""
        return self.query_one(
            "SELECT * FROM company_fetch_request WHERE stk_cd=%s "
            "AND status IN ('done','error') AND finished_at >= (NOW() - INTERVAL %s MINUTE) "
            "ORDER BY finished_at DESC LIMIT 1",
            (str(stk_cd), max(1, int(minutes))))

    def insert_fetch_request(self, stk_cd: str, stk_nm: str | None = None,
                             source: str = "fundamentals_filter") -> int:
        return self.insert(
            "INSERT INTO company_fetch_request (stk_cd, stk_nm, source) VALUES (%s,%s,%s)",
            (str(stk_cd)[:12], _trim_masked(stk_nm, 60), str(source)[:50] if source else None))

    def pending_fetch_request(self) -> dict | None:
        """가장 오래된 pending 요청 1건(claim 전 조회)."""
        return self.query_one(
            "SELECT * FROM company_fetch_request WHERE status='pending' "
            "ORDER BY requested_at, id LIMIT 1")

    def count_processing_fetch_requests(self) -> int:
        return int(self.scalar(
            "SELECT COUNT(*) AS n FROM company_fetch_request WHERE status='processing'",
            default=0) or 0)

    def claim_fetch_request(self, max_tries: int = 5) -> dict | None:
        """pending 요청 1건을 **원자적으로** claim 한다. 못 잡으면 None.

        `trend_scan_request` 와 동일하게 `UPDATE ... WHERE id=%s AND status='pending'`
        의 영향 행 수로 승자를 가린다(0이면 남이 가져간 것 → 다음 건으로).
        """
        for _ in range(max(1, int(max_tries))):
            row = self.pending_fetch_request()
            if row is None:
                return None
            won = self.execute(
                "UPDATE company_fetch_request SET status='processing', claimed_at=%s "
                "WHERE id=%s AND status='pending'", (now_kst(), int(row["id"])))
            if won:
                out = dict(row)
                out["status"] = "processing"
                return out
        return None

    def finish_fetch_request(self, request_id: int, *, status: str,
                             error_msg: str | None = None) -> int:
        return self.execute(
            "UPDATE company_fetch_request SET status=%s, error_msg=%s, finished_at=%s "
            "WHERE id=%s",
            (status if status in ("pending", "processing", "done", "error") else "error",
             _trim_masked(error_msg, 255), now_kst(), int(request_id)))

    def expire_stale_fetch_requests(self, minutes: int = 10,
                                    reason: str = "서버 재시작으로 중단") -> int:
        """`processing` 인 채 오래 멈춰 있는 요청을 `error` 로 정리한다(서버가 처리 중 죽은 경우)."""
        return self.execute(
            "UPDATE company_fetch_request SET status='error', error_msg=%s, finished_at=%s "
            "WHERE status='processing' AND requested_at < (NOW() - INTERVAL %s MINUTE)",
            (_trim_masked(reason, 255), now_kst(), max(1, int(minutes))))

    # ================================================================== #
    # 웹의 자동거래 시작/중지 명령 큐 (관리자 전용 + REAL 재확인, 2026-09-23)
    #
    # stock_web 계정은 `auto_trading_command` 에 pending 행을 INSERT 만 하고,
    # 엔진이 폴링해 원자적으로 claim 한 뒤 start_auto_trading()/stop_auto_trading()
    # 을 그대로 호출한다(게이트·기존 안전장치는 이 메서드들이 건드리지 않는다).
    # `trend_scan_request`/`company_fetch_request` 와 동일한 claim/finish 패턴이다.
    # ================================================================== #
    def pending_auto_trading_command(self) -> dict | None:
        """가장 오래된 pending 명령 1건(claim 전 조회)."""
        return self.query_one(
            "SELECT * FROM auto_trading_command WHERE status='pending' "
            "ORDER BY requested_at, id LIMIT 1")

    def claim_auto_trading_command(self, max_tries: int = 5) -> dict | None:
        """pending 명령 1건을 **원자적으로** claim 한다. 못 잡으면 None.

        `UPDATE ... WHERE id=%s AND status='pending'` 의 영향 행 수로 승자를 가린다
        (0이면 남이 가져간 것 → 다음 건으로) - trend_scan_request 와 동일한 방식.
        """
        for _ in range(max(1, int(max_tries))):
            row = self.pending_auto_trading_command()
            if row is None:
                return None
            won = self.execute(
                "UPDATE auto_trading_command SET status='processing', claimed_at=%s "
                "WHERE id=%s AND status='pending'", (now_kst(), int(row["id"])))
            if won:
                out = dict(row)
                out["status"] = "processing"
                return out
        return None

    def finish_auto_trading_command(self, cmd_id: int, status: str, message: str) -> None:
        self.execute(
            "UPDATE auto_trading_command SET status=%s, result_message=%s, handled_at=%s "
            "WHERE id=%s",
            (status if status in ("pending", "processing", "done", "error") else "error",
             _trim_masked(message, 255), now_kst(), int(cmd_id)))

    def expire_stale_auto_trading_commands(self, max_minutes: int = 5) -> int:
        """`processing` 인 채 오래 멈춰 있는 명령을 `error` 로 정리한다(서버가 처리 중 죽은 경우)."""
        return self.execute(
            "UPDATE auto_trading_command SET status='error', "
            "result_message=%s, handled_at=%s "
            "WHERE status='processing' AND requested_at < (NOW() - INTERVAL %s MINUTE)",
            ("서버 재시작으로 중단", now_kst(), max(1, int(max_minutes))))

    # ================================================================== #
    # 주문 / 체결 / 거래내역
    # ================================================================== #
    def start_run(self, env: str, order_enabled: bool, note: str | None = None) -> int:
        return self.insert(
            "INSERT INTO algo_run (started_at, env, order_enabled, note) VALUES (%s,%s,%s,%s)",
            (now_kst(), env, 1 if order_enabled else 0, (note or "")[:255] or None),
        )

    def end_run(self, run_id: int, note: str | None = None) -> None:
        if note:
            self.execute("UPDATE algo_run SET ended_at=%s, note=CONCAT(COALESCE(note,''),%s) WHERE id=%s",
                         (now_kst(), (" | " + note)[:200], run_id))
        else:
            self.execute("UPDATE algo_run SET ended_at=%s WHERE id=%s", (now_kst(), run_id))

    # NOT NULL 컬럼 기본값 (외부 관측 주문도 안전하게 적재)
    _ORDER_DEFAULTS = {
        "order_kind": "NEW", "dmst_stex_tp": "KRX", "trde_tp": "0", "ord_qty": 0,
        "side": "BUY", "status": "SENT", "filled_qty": 0, "is_dry_run": 0, "stk_cd": "",
    }

    def insert_order(self, **f) -> int:
        """주문 1행 기록.

        `signal_price`/`signal_context`/`params_snapshot`/`reject_reason` 는 사후 분석용
        선택 컬럼이다(주지 않으면 NULL — 기존 호출부와 호환).
        """
        cols = ("account_id", "run_id", "algo_code", "ord_no", "orig_ord_no", "side", "order_kind",
                "stk_cd", "stk_nm", "dmst_stex_tp", "trde_tp", "ord_qty", "ord_uv", "status",
                "filled_qty", "avg_fill_pric", "reason", "return_code", "return_msg", "is_dry_run",
                "signal_price", "signal_context", "params_snapshot", "reject_reason")
        vals = []
        for c in cols:
            v = f.get(c)
            if v is None and c in self._ORDER_DEFAULTS:
                v = self._ORDER_DEFAULTS[c]
            vals.append(v)
        sql = (f"INSERT INTO orders ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})")
        return self.insert(sql, vals)

    def update_order(self, order_id: int, **f) -> int:
        allowed = ("ord_no", "status", "filled_qty", "avg_fill_pric", "return_code", "return_msg",
                   "is_dry_run", "reject_reason")
        sets = {k: v for k, v in f.items() if k in allowed}
        if not sets:
            return 0
        sql = "UPDATE orders SET " + ", ".join(f"{k}=%s" for k in sets) + " WHERE id=%s"
        return self.execute(sql, list(sets.values()) + [order_id])

    # -- order_event (주문 상태 변화 이력 · 영구 보관) -------------------- #
    _ORDER_EVENT_COLS = ("order_id", "account_id", "ord_no", "event_time", "event_type", "status",
                         "filled_qty", "remain_qty", "price", "reject_reason", "return_code",
                         "message", "source")

    def insert_order_event(self, **f) -> int:
        """주문 상태 변화 1건 기록.

        `orders` 는 최신 상태만 들고 있으므로, 접수→부분체결→체결/거부까지의 **경로**는
        이 테이블에만 남는다(7일 정리 대상이 아님).
        """
        vals = []
        for c in self._ORDER_EVENT_COLS:
            v = f.get(c)
            if c == "event_time" and v is None:
                v = now_kst()
            elif c == "source" and not v:
                v = "EXECUTOR"
            elif c in ("message", "reject_reason") and v is not None:
                v = mask_text(str(v))[:255] or None
            elif c in ("status", "ord_no") and v is not None:
                v = str(v)[:20]
            vals.append(v)
        sql = (f"INSERT INTO order_event ({', '.join(self._ORDER_EVENT_COLS)}) "
               f"VALUES ({', '.join(['%s'] * len(self._ORDER_EVENT_COLS))})")
        return self.insert(sql, vals)

    def order_events(self, order_id: int) -> list[dict]:
        return self.query(
            "SELECT * FROM order_event WHERE order_id=%s ORDER BY id", (order_id,))

    def find_order_by_ordno(self, account_id: int, ord_no: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM orders WHERE account_id=%s AND ord_no=%s ORDER BY id DESC LIMIT 1",
            (account_id, ord_no))

    def upsert_order_by_ordno(self, account_id: int, ord_no: str, **f) -> int:
        """외부(REST/WS)에서 관측한 주문을 반영. 없으면 새로 만든다."""
        existing = self.find_order_by_ordno(account_id, ord_no)
        if existing:
            self.update_order(existing["id"], **f)
            return existing["id"]
        payload = dict(f)
        payload.setdefault("side", "BUY")
        payload.setdefault("trde_tp", "0")
        payload.setdefault("ord_qty", 0)
        payload.setdefault("status", "ACCEPTED")
        payload["account_id"] = account_id
        payload["ord_no"] = ord_no
        return self.insert_order(**payload)

    def count_orders_today(self, account_id: int, only_sent: bool = True) -> int:
        """당일 주문 건수.

        `only_sent=True` 는 **우리 알고리즘이 실제로 낸 신규 주문만** 센다 (R-15).
        수동(HTS) 주문이나 동기화로 들어온 외부 주문(algo_code IS NULL)·정정/취소는
        일 주문 횟수 한도를 소진하지 않는다.
        """
        sql = ("SELECT COUNT(*) FROM orders WHERE account_id=%s AND DATE(created_at)=CURDATE()")
        if only_sent:
            sql += " AND is_dry_run=0 AND algo_code IS NOT NULL AND order_kind='NEW'"
        return int(self.scalar(sql, (account_id,), default=0) or 0)

    def count_new_entries_today(self, account_id: int, algo_code: str | None = None,
                                only_sent: bool = True) -> int:
        """당일 신규 진입 종목 수. 기본적으로 **실제 전송된 주문만** 센다(B3)."""
        sql = ("SELECT COUNT(DISTINCT stk_cd) FROM orders WHERE account_id=%s AND side='BUY' "
               "AND DATE(created_at)=CURDATE()")
        if only_sent:
            sql += " AND is_dry_run=0"
        args: list = [account_id]
        if algo_code:
            sql += " AND algo_code=%s"
            args.append(algo_code)
        return int(self.scalar(sql, args, default=0) or 0)

    def last_order_at(self, account_id: int, stk_cd: str, side: str | None = None) -> _dt.datetime | None:
        sql = "SELECT MAX(created_at) FROM orders WHERE account_id=%s AND stk_cd=%s"
        args: list = [account_id, stk_cd]
        if side:
            sql += " AND side=%s"
            args.append(side)
        return self.scalar(sql, args)

    def count_open_orders(self, account_id: int) -> int:
        """미체결(전송됨/접수/부분체결) 주문 건수. 자동거래 중지 안내에 사용."""
        return int(self.scalar(
            "SELECT COUNT(*) FROM orders WHERE account_id=%s AND is_dry_run=0 "
            "AND status IN ('SENT','ACCEPTED','PARTIAL')", (account_id,), default=0) or 0)

    def has_open_order(self, account_id: int, stk_cd: str, side: str | None = None) -> bool:
        """동일 종목 미체결 주문 존재 여부.

        `side` 를 주면 **같은 방향**만 본다 (R-07). 매수 미체결이 손절 매도를 막지 않게 한다.
        """
        sql = ("SELECT COUNT(*) FROM orders WHERE account_id=%s AND stk_cd=%s "
               "AND status IN ('SENT','ACCEPTED','PARTIAL')")
        args: list = [account_id, stk_cd]
        if side:
            sql += " AND side=%s"
            args.append(side)
        return bool(self.scalar(sql, args, default=0))

    def expire_unknown_sent_orders(self, account_id: int, minutes: int = 10) -> int:
        """주문번호를 못 받은 채 SENT 로 남은 주문을 FAILED 로 정리한다 (R-07).

        접수 여부가 끝내 확정되지 않은 주문이 영원히 '미체결'로 남아 해당 종목을
        영구 차단하는 것을 막는다.
        """
        return self.execute(
            "UPDATE orders SET status='FAILED', return_msg='UNKNOWN 확정 불가' "
            "WHERE account_id=%s AND ord_no IS NULL AND status='SENT' AND is_dry_run=0 "
            "AND created_at < (NOW() - INTERVAL %s MINUTE)",
            (account_id, max(1, int(minutes))))

    def count_executions_since(self, account_id: int, stk_cd: str, since: _dt.datetime) -> int:
        """특정 시각 이후 해당 종목의 체결 건수 (접수여부 불명 주문의 증거 확인용 - R-03)."""
        return int(self.scalar(
            "SELECT COUNT(*) FROM executions WHERE account_id=%s AND stk_cd=%s "
            "AND executed_at >= %s", (account_id, stk_cd, since), default=0) or 0)

    def order_algo_code(self, account_id: int, ord_no: str) -> str | None:
        """주문번호가 우리 알고리즘 주문인지 확인 (R-04). 외부/수동 주문이면 None."""
        return self.scalar(
            "SELECT algo_code FROM orders WHERE account_id=%s AND ord_no=%s "
            "ORDER BY id DESC LIMIT 1", (account_id, ord_no))

    def execution_exists(self, account_id: int, ord_no: str, cntr_no: str) -> bool:
        """이 (계좌, 주문번호, 체결번호) 체결이 이미 저장돼 있는지 (체결 알림 메일 중복발송 방지용).

        `upsert_execution` 의 `only_if_absent` 중복확인(ord_no+qty+pric)보다 더 정밀한
        키(cntr_no 포함)로, **upsert 호출 전에** 먼저 확인해 '진짜 새 체결'만 알림을
        내보내기 위한 것이다 (WS 재연결 재수신 / REST 재조회로 인한 중복 메일 방지).
        """
        return bool(self.scalar(
            "SELECT COUNT(*) FROM executions WHERE account_id=%s AND ord_no=%s AND cntr_no=%s",
            (account_id, ord_no, cntr_no or ""), default=0))

    def execution_exists_by_amount(self, account_id: int, ord_no: str, cntr_qty: int,
                                   cntr_pric: int) -> bool:
        """`upsert_execution` 의 `only_if_absent` 중복확인과 완전히 같은 조건(ord_no+qty+pric).

        REST(ka10076)는 실제 체결번호가 없어 결정적으로 만든 키(execution_key)를
        `cntr_no` 자리에 쓰므로, WS(909, 실체결번호)가 먼저 기록한 같은 체결을 REST 가
        나중에 재관측할 때는 `cntr_no` 값이 서로 다르다. `execution_exists()`(cntr_no
        정밀비교) 만으로는 이 교차(WS↔REST) 중복을 못 잡으므로, 메일발송 여부 판단에는
        이 조건도 함께 확인한다(둘 중 하나라도 있으면 '이미 있음').
        """
        return bool(self.scalar(
            "SELECT COUNT(*) FROM executions WHERE account_id=%s AND ord_no=%s "
            "AND cntr_qty=%s AND cntr_pric=%s",
            (account_id, ord_no, cntr_qty, cntr_pric), default=0))

    def upsert_execution(self, account_id: int, ord_no: str, cntr_no: str, stk_cd: str, stk_nm: str | None,
                         side: str, cntr_qty: int, cntr_pric: int, executed_at: _dt.datetime,
                         cmsn: int | None = None, tax: int | None = None, source: str = "WS",
                         only_if_absent: bool = False) -> None:
        """체결 upsert. `only_if_absent=True` 면 같은 주문의 동일 체결이 이미 있으면 건너뛴다.

        WS(체결번호 909)가 정본이고 REST(ka10076)는 보정이므로, REST 는 이미 저장된
        같은 (주문번호, 가격, 수량) 체결을 덮어쓰지 않는다 (B2).
        """
        if only_if_absent:
            dup = self.scalar(
                "SELECT COUNT(*) FROM executions WHERE account_id=%s AND ord_no=%s "
                "AND cntr_qty=%s AND cntr_pric=%s",
                (account_id, ord_no, cntr_qty, cntr_pric), default=0)
            if dup:
                return
        self.execute(
            "INSERT INTO executions (account_id, ord_no, cntr_no, stk_cd, stk_nm, side, cntr_qty, cntr_pric, "
            "cmsn, tax, executed_at, source) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE cntr_qty=VALUES(cntr_qty), cntr_pric=VALUES(cntr_pric), "
            "cmsn=VALUES(cmsn), tax=VALUES(tax), executed_at=VALUES(executed_at)",
            (account_id, ord_no, cntr_no or "", stk_cd, (stk_nm or None), side, cntr_qty, cntr_pric,
             cmsn, tax, executed_at, source),
        )

    def upsert_trade_ledger(self, account_id: int, rows: list[dict]) -> int:
        sql = (
            "INSERT INTO trade_ledger (account_id, trde_dt, trde_no, trde_kind_nm, rmrk_nm, stk_cd, stk_nm, "
            "trde_qty, trde_unit, trde_amt, cmsn, tax, exct_amt, entra_remn, proc_tm) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE trde_kind_nm=VALUES(trde_kind_nm), rmrk_nm=VALUES(rmrk_nm), "
            "stk_cd=VALUES(stk_cd), stk_nm=VALUES(stk_nm), trde_qty=VALUES(trde_qty), "
            "trde_unit=VALUES(trde_unit), trde_amt=VALUES(trde_amt), cmsn=VALUES(cmsn), tax=VALUES(tax), "
            "exct_amt=VALUES(exct_amt), entra_remn=VALUES(entra_remn), proc_tm=VALUES(proc_tm)"
        )
        params = [
            (account_id, r.get("trde_dt"), r.get("trde_no"), r.get("trde_kind_nm"), r.get("rmrk_nm"),
             r.get("stk_cd"), r.get("stk_nm"), r.get("trde_qty"), r.get("trde_unit"), r.get("trde_amt"),
             r.get("cmsn"), r.get("tax"), r.get("exct_amt"), r.get("entra_remn"), r.get("proc_tm"))
            for r in rows if r.get("trde_dt") and r.get("trde_no")
        ]
        return self.execute_many(sql, params)

    def upsert_daily_summary(self, account_id: int, base_dt: _dt.date, rows: list[dict]) -> int:
        sql = (
            "INSERT INTO daily_trade_summary (account_id, base_dt, stk_cd, stk_nm, buy_qty, buy_avg_pric, "
            "buy_amt, sell_qty, sell_avg_pric, sell_amt, cmsn_tax, pl_amt, prft_rt) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE stk_nm=VALUES(stk_nm), buy_qty=VALUES(buy_qty), "
            "buy_avg_pric=VALUES(buy_avg_pric), buy_amt=VALUES(buy_amt), sell_qty=VALUES(sell_qty), "
            "sell_avg_pric=VALUES(sell_avg_pric), sell_amt=VALUES(sell_amt), cmsn_tax=VALUES(cmsn_tax), "
            "pl_amt=VALUES(pl_amt), prft_rt=VALUES(prft_rt)"
        )
        params = [
            (account_id, base_dt, r.get("stk_cd"), r.get("stk_nm"), r.get("buy_qty"), r.get("buy_avg_pric"),
             r.get("buy_amt"), r.get("sell_qty"), r.get("sell_avg_pric"), r.get("sell_amt"),
             r.get("cmsn_tax"), r.get("pl_amt"), r.get("prft_rt"))
            for r in rows if r.get("stk_cd")
        ]
        return self.execute_many(sql, params)

    def today_realized_pl(self, account_id: int, base_dt: _dt.date) -> int:
        return int(self.scalar(
            "SELECT COALESCE(SUM(pl_amt),0) FROM daily_trade_summary WHERE account_id=%s AND base_dt=%s",
            (account_id, base_dt), default=0) or 0)

    def today_realized_pl_from_executions(self, account_id: int, base_dt: _dt.date) -> int:
        """장중 추정 손익 (B6): 당일 체결의 (매도금액 - 매수금액 - 수수료/세금).

        매매일지(ka10170)가 장마감 후에만 채워지므로 장중에는 이 추정치를 함께 본다.
        """
        row = self.query_one(
            "SELECT "
            " COALESCE(SUM(CASE WHEN side='SELL' THEN cntr_qty*cntr_pric ELSE 0 END),0) AS sell_amt,"
            " COALESCE(SUM(CASE WHEN side='BUY'  THEN cntr_qty*cntr_pric ELSE 0 END),0) AS buy_amt,"
            " COALESCE(SUM(COALESCE(cmsn,0)+COALESCE(tax,0)),0) AS fees "
            "FROM executions WHERE account_id=%s AND DATE(executed_at)=%s",
            (account_id, base_dt))
        if not row:
            return 0
        sell = int(row.get("sell_amt") or 0)
        buy = int(row.get("buy_amt") or 0)
        fees = int(row.get("fees") or 0)
        if sell <= 0:
            return 0          # 매도가 없으면 실현손익 없음(매수는 평가손익)
        return sell - buy - fees

    # ================================================================== #
    # 시스템 / 관제 / 로그
    # ================================================================== #
    def set_status(self, component: str, status: str, message: str | None = None) -> None:
        self.execute(
            "INSERT INTO server_status (component, status, message) VALUES (%s,%s,%s) "
            "ON DUPLICATE KEY UPDATE status=VALUES(status), message=VALUES(message), "
            "updated_at=CURRENT_TIMESTAMP",
            (component, status, mask_text(message or "")[:255] or None),
        )

    def log_event(self, level: str, category: str, message: str) -> int:
        """`event_log`(7일 보관) 기록 + 주요 이벤트는 `event_archive` 에 영구 보관."""
        lv = level.upper()
        cat = category[:30]
        msg = mask_text(message)[:500]
        row_id = self.insert(
            "INSERT INTO event_log (level, category, message) VALUES (%s,%s,%s)",
            (lv, cat, msg),
        )
        self._archive_event(lv, cat, msg)
        return row_id

    def _archive_event(self, level: str, category: str, message: str) -> None:
        """규칙에 맞는 이벤트만 `event_archive` 에도 남긴다.

        아카이브 기록 실패는 본 기록(event_log)에 영향을 주지 않는다.
        """
        try:
            if not should_archive_event(level, category, message):
                return
            if not self._archive_allow_now(level, category, message):
                return      # 60초 내 동일 메시지 반복 → 1건만
            self.execute(
                "INSERT INTO event_archive (level, category, message) VALUES (%s,%s,%s)",
                (level, category, message),
            )
        except Exception:  # noqa: BLE001 - 보관본 실패가 본 기록을 막지 않는다
            log.debug("event_archive 기록 실패", exc_info=True)

    def _archive_allow_now(self, level: str, category: str, message: str) -> bool:
        """같은 (레벨, 분류, 메시지) 의 연속 중복을 60초에 1건으로 줄인다."""
        key = (level, category, message)
        now = _dt.datetime.now()
        with self._archive_lock:
            last = self._archive_seen.get(key)
            if last is not None and (now - last).total_seconds() < ARCHIVE_DEDUP_SEC:
                return False
            self._archive_seen[key] = now
            if len(self._archive_seen) > 500:
                cutoff = now - _dt.timedelta(seconds=ARCHIVE_DEDUP_SEC)
                for k, ts in list(self._archive_seen.items()):
                    if ts < cutoff:
                        self._archive_seen.pop(k, None)
        return True

    def recent_events(self, limit: int = 200, min_level: str | None = None) -> list[dict]:
        order = ("DEBUG", "INFO", "WARN", "ERROR")
        sql = "SELECT * FROM event_log"
        args: list = []
        if min_level and min_level.upper() in order:
            keep = order[order.index(min_level.upper()):]
            sql += " WHERE level IN (" + ",".join(["%s"] * len(keep)) + ")"
            args += list(keep)
        sql += " ORDER BY id DESC LIMIT %s"
        args.append(int(limit))
        return list(reversed(self.query(sql, args)))

    def log_api_call(self, api_id: str, http_status: int | None, return_code: int | None,
                     return_msg: str, elapsed_ms: int) -> None:
        """`api_call_log`(7일 보관) 기록 + 오류 응답은 `api_error_log` 에 영구 보관."""
        aid = api_id[:10]
        msg = (return_msg or "")[:255] or None
        self.execute(
            "INSERT INTO api_call_log (api_id, http_status, return_code, return_msg, elapsed_ms) "
            "VALUES (%s,%s,%s,%s,%s)",
            (aid, http_status, return_code, msg, elapsed_ms),
        )
        if should_log_api_error(http_status, return_code):
            try:
                self.execute(
                    "INSERT INTO api_error_log (api_id, http_status, return_code, return_msg, "
                    "elapsed_ms) VALUES (%s,%s,%s,%s,%s)",
                    (aid, http_status, return_code, msg, elapsed_ms),
                )
            except Exception:  # noqa: BLE001 - 보관본 실패가 본 기록을 막지 않는다
                log.debug("api_error_log 기록 실패", exc_info=True)

    def purge_old(self, retention_days: int = 7) -> dict[str, int]:
        """보관기간 초과 로그 삭제.

        대상은 `event_log`/`api_call_log`/`screening_result` **뿐**이다.
        주문·체결·신호·주문이벤트 등 거래 원장은 여기서 절대 지우지 않는다.
        """
        days = max(1, int(retention_days))
        out = {}
        out["event_log"] = self.execute(
            "DELETE FROM event_log WHERE created_at < (NOW() - INTERVAL %s DAY)", (days,))
        out["api_call_log"] = self.execute(
            "DELETE FROM api_call_log WHERE created_at < (NOW() - INTERVAL %s DAY)", (days,))
        out["screening_result"] = self.execute(
            "DELETE FROM screening_result WHERE captured_at < (NOW() - INTERVAL %s DAY)", (days * 4,))
        return out

    def archive_retention_days(self) -> int:
        """`system_setting.archive_retention_days` (기본 365일, 하한 30일)."""
        try:
            days = int(str(self.get_setting(
                "archive_retention_days", str(ARCHIVE_RETENTION_DEFAULT))).strip())
        except (TypeError, ValueError):
            days = ARCHIVE_RETENTION_DEFAULT
        return max(ARCHIVE_RETENTION_MIN, days)

    def purge_archives(self, days: int | None = None) -> dict[str, int]:
        """영구 보관본 정리 — `event_archive` / `api_error_log` **만** 대상이다.

        `order_event`, `orders`, `executions`, `signal_log`, `llm_decision_log`,
        `position_state` 는 어떤 경로로도 삭제하지 않는다(거래 분석 원장).
        """
        d = self.archive_retention_days() if days is None else max(
            ARCHIVE_RETENTION_MIN, int(days))
        out = {}
        out["event_archive"] = self.execute(
            "DELETE FROM event_archive WHERE created_at < (NOW() - INTERVAL %s DAY)", (d,))
        out["api_error_log"] = self.execute(
            "DELETE FROM api_error_log WHERE created_at < (NOW() - INTERVAL %s DAY)", (d,))
        return out

    # ================================================================== #
    # 인증 (app_user, 웹과 공유) - 자동거래 시작 확인창의 아이디/비밀번호 재확인용
    # ================================================================== #
    def verify_login(self, username: str, password: str) -> tuple[bool, str]:
        """app_user 자격증명 재확인(웹과 동일 계정/해시 공유). (통과여부, 사유|role) 반환.

        - 계정이 없어도 더미 해시로 동일하게 bcrypt 검증을 수행해 타이밍으로 계정 존재
          여부가 드러나지 않게 한다(web/lib/auth.php 의 AUTH_DUMMY_HASH 와 동일한 방어 -
          `_check_password` 호출은 계정 존재 여부와 무관하게 항상 먼저 실행된다).
        - is_active=0 이거나 locked_until 이 현재보다 미래면 거부한다.
        - **주의**: `app_user.failed_count`/`locked_until` 은 이 메서드에서 절대 갱신하지
          않는다 - 그 락아웃 로직은 web/lib/auth.php 의 웹 로그인 전용이며, 여기서는
          자동거래 시작 확인창의 순수 읽기 전용 재확인만 한다.
        - 비밀번호 값 자체는 반환값·로그·예외 어디에도 남기지 않는다.

        반환: 성공 시 `(True, role)` (예: "admin"), 실패 시
        `(False, "계정 없음"|"비활성 계정"|"잠긴 계정"|"비밀번호 불일치")`.
        """
        uname = str(username or "").strip()
        row = None
        if uname:
            row = self.query_one(
                "SELECT id, username, password_hash, display_name, role, is_active, "
                "locked_until FROM app_user WHERE username=%s", (uname,))
        stored_hash = row["password_hash"] if row else _DUMMY_PASSWORD_HASH
        # 계정이 있든 없든 항상 bcrypt 검증을 수행한다(타이밍 사이드채널 방지).
        verified = _check_password(stored_hash, password)
        if row is None:
            return False, "계정 없음"
        if not verified:
            return False, "비밀번호 불일치"
        if not row.get("is_active"):
            return False, "비활성 계정"
        locked_until = row.get("locked_until")
        if locked_until is not None and locked_until > now_kst():
            return False, "잠긴 계정"
        return True, str(row.get("role") or "viewer")
