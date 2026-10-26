# -*- coding: utf-8 -*-
"""
일회성: ka10170(당일매매일지) 과거 일자 재수집 → daily_trade_summary 백필.

  python tools/backfill_daily_diary.py                       # dry-run(기본): 조회·비교만, DB 쓰기 없음
  python tools/backfill_daily_diary.py --apply               # 실제 upsert
  python tools/backfill_daily_diary.py --apply --delete-stale  # + 키움이 "조회 내역 없음"이라 답한
                                                                #   날짜/종목의 기존 행 삭제
  옵션: --start YYYY-MM-DD --end YYYY-MM-DD --account-id N --sleep 0.4

배경
  * 2026-10-26 까지 sync_daily_diary 가 ottks_tp="1"(당일매수에 대한 당일매도)로 호출해
    전일 이전 매수분을 판 매도 행의 pl_amt 가 0 으로 저장됐다. "2"(당일매도 전체)로 다시 받는다.
  * 평일 휴장일에 장마감 정리가 돌면서 전 거래일 데이터가 휴장일 날짜로 한 번 더 저장된
    유령 행이 있다(2026-10-05, 10-09). 지금 그 날짜를 조회하면 키움은 "조회 내역이 없습니다"를
    준다 → --delete-stale 일 때만 삭제한다.

안전
  * 쓰는 표는 **daily_trade_summary 하나뿐**이다. orders/executions/holding/position_state 등
    주문·체결·잔고 표와 system_setting(게이트) 은 읽지도 쓰지도 않는다(account 는 id 조회만).
  * 키움은 읽기 전용 TR ka10170 만 호출한다. 주문 API 경로 없음.
  * 운영 중인 서버와 별개로 토큰을 새로 발급하고, 폐기하지 않는다(서버 세션 보호 -
    `--trend-scan-check` 와 같은 방식). API 호출 로그(api_call_log)도 남기지 않는다.
  * 같은 (account_id, base_dt, stk_cd) UNIQUE KEY 로 upsert 하므로 여러 번 돌려도 결과가 같다.
  * 날짜 하나가 실패해도 건너뛰고 끝까지 진행한 뒤 실패 목록을 출력한다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from stock_svr.config import load_config  # noqa: E402
from stock_svr.db import Database  # noqa: E402
from stock_svr.kiwoom.auth import TokenManager  # noqa: E402
from stock_svr.kiwoom.rest import KiwoomRest  # noqa: E402
from stock_svr.services.housekeeping import HousekeepingService  # noqa: E402

NO_DATA_MSG = "조회 내역이 없습니다"
OUT_OF_RANGE_MSG = "최근 2개월"


class _CapturingRest:
    """KiwoomRest 를 감싸 ka10170 원응답만 기록한다(그 밖의 API 는 거부)."""

    def __init__(self, rest: KiwoomRest):
        self._rest = rest
        self.last_pages: list[dict] = []

    def call_paged(self, api_id, body=None, **kw):
        if api_id != "ka10170":
            raise RuntimeError(f"백필 스크립트는 ka10170 만 호출합니다 (요청: {api_id})")
        self.last_pages = self._rest.call_paged(api_id, body, **kw)
        return self.last_pages


class _DryRunDb:
    """dry-run 용: upsert 를 가로채 행만 모으고 DB 에 쓰지 않는다."""

    def __init__(self):
        self.rows: list[dict] = []

    def upsert_daily_summary(self, account_id, base_dt, rows):
        self.rows = list(rows)
        return len(rows)


def _summary(db: Database, account_id: int) -> dict:
    return db.query_one(
        "SELECT COUNT(*) n, COALESCE(SUM(pl_amt),0) pl, COALESCE(SUM(sell_amt),0) sell, "
        "COALESCE(SUM(buy_amt),0) buy, COALESCE(SUM(cmsn_tax),0) fee "
        "FROM daily_trade_summary WHERE account_id=%s", (account_id,)) or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="실제로 DB 에 upsert")
    ap.add_argument("--delete-stale", action="store_true",
                    help="키움이 '조회 내역 없음'이라 답한 날짜·종목의 기존 행 삭제(--apply 필요)")
    ap.add_argument("--start", help="시작일 YYYY-MM-DD (기본: 기록상 가장 이른 날)")
    ap.add_argument("--end", help="종료일 YYYY-MM-DD (기본: 어제)")
    ap.add_argument("--account-id", type=int)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--json-out", help="날짜별 결과 JSON 저장 경로")
    args = ap.parse_args()

    cfg = load_config(None)
    db = Database(cfg.db)
    if not db.ping():
        print("DB 접속 실패:", db.last_error)
        return 1

    settings = db.get_settings()
    env = "mock" if (settings.get("trading_mode") or "real").lower() == "mock" else "real"
    if args.account_id:
        account_id = args.account_id
    else:
        accts = db.query("SELECT id FROM account WHERE env=%s", (env,))
        if len(accts) != 1:
            print(f"env={env} 계좌가 {len(accts)}개입니다. --account-id 로 지정하세요.")
            return 1
        account_id = int(accts[0]["id"])

    if args.start:
        start = dt.date.fromisoformat(args.start)
    else:
        cands = [db.scalar(q, (account_id,)) for q in (
            "SELECT MIN(base_dt) FROM daily_trade_summary WHERE account_id=%s",
            "SELECT MIN(DATE(created_at)) FROM orders WHERE account_id=%s",
            "SELECT MIN(DATE(executed_at)) FROM executions WHERE account_id=%s",
        )]
        cands = [c for c in cands if c]
        if not cands:
            print("기록이 없습니다.")
            return 0
        start = min(cands)
    end = dt.date.fromisoformat(args.end) if args.end else dt.date.today() - dt.timedelta(days=1)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== ka10170 백필 [{mode}] account_id={account_id} env={env} {start} ~ {end} "
          f"(ottks_tp=2, delete_stale={args.delete_stale and args.apply}) ===")
    before = _summary(db, account_id)
    print("BEFORE:", before)

    tokens = TokenManager(cfg.kiwoom, env, cfg.kiwoom.http_timeout_sec)
    rest = KiwoomRest(cfg.kiwoom, env, tokens, on_call=None)
    cap = _CapturingRest(rest)
    failed: list[tuple[str, str]] = []
    report: list[dict] = []
    try:
        tokens.get_token()
        d = start
        while d <= end:
            day = d
            d += dt.timedelta(days=1)
            if day.weekday() >= 5:
                continue
            old = {r["stk_cd"]: r for r in db.query(
                "SELECT * FROM daily_trade_summary WHERE account_id=%s AND base_dt=%s",
                (account_id, day))}
            try:
                # 서비스의 요청·파싱 경로를 그대로 쓰되 upsert 는 가로채 행만 받는다
                probe = _DryRunDb()
                HousekeepingService(probe, cap, account_id).sync_daily_diary(day)
                new_rows = probe.rows
                msg = " ".join((p.get("return_msg") or "").strip() for p in cap.last_pages)
                if args.apply and new_rows and OUT_OF_RANGE_MSG not in msg:
                    db.upsert_daily_summary(account_id, day, new_rows)
            except Exception as exc:  # noqa: BLE001 - 날짜 하나 실패로 멈추지 않는다
                failed.append((day.isoformat(), f"{type(exc).__name__}: {exc}"))
                print(f"  {day} FAIL {type(exc).__name__}: {exc}")
                time.sleep(args.sleep)
                continue
            if OUT_OF_RANGE_MSG in msg:
                failed.append((day.isoformat(), f"조회기간 초과: {msg}"))
                print(f"  {day} SKIP 조회기간 초과 ({msg})")
                time.sleep(args.sleep)
                continue
            new = {r["stk_cd"]: r for r in new_rows}
            changed = []
            for code, r in new.items():
                o = old.get(code)
                if o is None:
                    changed.append((code, "NEW", None, r.get("pl_amt")))
                elif any((o.get(k) or 0) != (r.get(k) or 0) for k in (
                        "buy_qty", "buy_amt", "sell_qty", "sell_amt", "cmsn_tax", "pl_amt")):
                    changed.append((code, "UPD", o.get("pl_amt"), r.get("pl_amt")))
            # 키움 응답에 없는 기존 행: '조회 내역 없음'일 때만 확정적으로 유령 행으로 본다
            stale = [c for c in old if c not in new]
            no_data = NO_DATA_MSG in msg and not new
            deleted = 0
            if stale and args.apply and args.delete_stale and (no_data or new):
                deleted = db.execute(
                    "DELETE FROM daily_trade_summary WHERE account_id=%s AND base_dt=%s AND stk_cd IN ("
                    + ",".join(["%s"] * len(stale)) + ")", (account_id, day, *stale))
            print(f"  {day} rows old={len(old)} new={len(new)} changed={len(changed)}"
                  f" stale={len(stale)}{' (deleted ' + str(deleted) + ')' if deleted else ''}"
                  f" msg='{msg}'")
            for code, kind, o_pl, n_pl in changed:
                print(f"      {kind} {code} pl {o_pl} -> {n_pl}")
            for code in stale:
                o = old[code]
                print(f"      STALE {code} {o.get('stk_nm')} buy={o.get('buy_amt')} "
                      f"sell={o.get('sell_amt')} pl={o.get('pl_amt')}")
            report.append({"date": day.isoformat(), "msg": msg, "old": len(old), "new": len(new),
                           "changed": changed, "stale": stale, "deleted": deleted})
            time.sleep(args.sleep)
    finally:
        rest.close()  # 토큰은 폐기하지 않는다(운영 서버 세션 보호)

    after = _summary(db, account_id)
    print("BEFORE:", before)
    print("AFTER :", after)
    print("FAILED:", failed or "(없음)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"before": before, "after": after, "failed": failed, "days": report},
            ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    db.close()
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
