"""stock_svr 진입점.

    python -m stock_svr                # GUI 실행(엔진 자동 시작)
    python -m stock_svr --check        # 읽기 전용 점검(설정→DB→토큰→ka00001/kt00001/kt00018)
    python -m stock_svr --smoke 20     # GUI 를 20초만 띄웠다 자동 종료(스모크)
    python -m stock_svr --eval-once    # 엔진 없이 평가 사이클 1회(관찰모드 진단, 주문 전송 없음)
    python -m stock_svr --claude-check # Claude 거부권 필터 점검(합성 데이터 2건, DB/키움 미사용)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from .config import ConfigError, load_config
from .db import Database
from .logging_setup import attach_db_handler, get_logger, setup_logging
from .single_instance import AlreadyRunningError, single_instance
from .util import mask_account_no, now_kst

log = get_logger("main")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="stock_svr", description="키움 REST 자동매매 서버모듈")
    p.add_argument("--config", help="설정 파일 경로 (기본: server/config/config.local.ini)")
    p.add_argument("--check", action="store_true", help="읽기 전용 점검 후 종료")
    p.add_argument("--claude-check", action="store_true",
                   help="Claude 거부권 필터 점검: 합성 종목 2건을 실제 Claude API 로 검토"
                        " (키움 API·DB 기록 없음)")
    p.add_argument("--smoke", type=float, metavar="SEC",
                   help="GUI 를 SEC 초만 띄웠다가 자동 종료(스모크 테스트)")
    p.add_argument("--eval-once", action="store_true",
                   help="엔진 루프 없이 평가 사이클 1회 실행 (**관찰 전용** - 주문을 전송하지 않음)")
    p.add_argument("--force-market", action="store_true",
                   help="--eval-once 에서 장외에도 평가를 강제(조회 전용, 관찰 전용은 그대로 유지)")
    p.add_argument("--allow-orders", action="store_true",
                   help="[효과 없음(이중 잠금)] --eval-once 의 관찰 전용 해제를 시도하지만, "
                        "자동거래 스위치가 기동 시 항상 '중지'라 주문은 전송되지 않습니다. "
                        "평상시 사용 금지")
    p.add_argument("--all-algos", action="store_true",
                   help="--eval-once 에서 선택되지 않은 알고리즘도 평가(진단용)")
    p.add_argument("--no-autostart", action="store_true", help="GUI 기동 시 엔진 자동 시작 안 함")
    p.add_argument("--log-level", help="로그 레벨 덮어쓰기 (DEBUG/INFO/WARN/ERROR)")
    return p


def _prepare(args) -> tuple:
    cfg = load_config(args.config)
    if args.log_level:
        cfg.logging.level = args.log_level.upper()
    setup_logging(cfg.logging, console=True, to_ui=True)
    db = Database(cfg.db)
    return cfg, db


# ====================================================================== #
def cmd_check(cfg, db: Database) -> int:
    """읽기 전용 점검. 주문 API 는 어떤 경로로도 호출하지 않는다."""
    from .kiwoom.auth import TokenManager
    from .kiwoom.rest import KiwoomRest
    from .services.sync_account import AccountService

    out = []
    ok = True

    def line(label: str, status: str, detail: str = ""):
        out.append(f"  [{status:^4}] {label:<20} {detail}")

    print(f"\n=== stock_svr --check ({now_kst():%Y-%m-%d %H:%M:%S} KST) ===")
    print(f"  설정 파일: {cfg.source_path}")

    # 1) DB
    if db.ping():
        line("DB 접속", "OK", f"{cfg.db.user}@{cfg.db.host}:{cfg.db.port}/{cfg.db.name}")
    else:
        line("DB 접속", "FAIL", db.last_error or "연결 실패")
        print("\n".join(out))
        return 1

    settings = db.get_settings()
    mode = (settings.get("trading_mode") or "real").lower()
    env = "mock" if mode == "mock" else "real"
    from .engine.context import OrderGateState
    gate = OrderGateState.from_settings(settings)
    line("거래 설정", "OK", gate.describe())

    # 2) 키 파일 존재 확인 (값은 출력하지 않는다)
    tokens = TokenManager(cfg.kiwoom, env, cfg.kiwoom.http_timeout_sec)
    try:
        cfg.kiwoom.read_keys(env)
        line("앱키/시크릿키", "OK", "설정 경로에서 읽기 성공 (값 비표시)")
    except ConfigError as exc:
        line("앱키/시크릿키", "FAIL", str(exc))
        print("\n".join(out))
        return 1

    rest = KiwoomRest(cfg.kiwoom, env, tokens, on_call=lambda *a: _safe_api_log(db, *a))
    try:
        # 3) 토큰
        try:
            tokens.get_token()
            line("접근토큰(au10001)", "OK", f"token={tokens.masked()} 만료={tokens.expires_at}")
        except Exception as exc:  # noqa: BLE001
            line("접근토큰(au10001)", "FAIL", f"{type(exc).__name__}: {exc}")
            print("\n".join(out))
            return 1

        svc = AccountService(db, rest)
        # 4) 계좌번호
        try:
            acct_no = svc.fetch_account_no()
            line("계좌조회(ka00001)", "OK" if acct_no else "WARN", mask_account_no(acct_no))
        except Exception as exc:  # noqa: BLE001
            acct_no = None
            ok = False
            line("계좌조회(ka00001)", "FAIL", f"{type(exc).__name__}: {exc}")

        # 5) 예수금
        try:
            dep = svc.fetch_deposit()
            line("예수금(kt00001)", "OK",
                 f"예수금={_m(dep.get('entr'))} 주문가능={_m(dep.get('ord_alow_amt'))}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            line("예수금(kt00001)", "FAIL", f"{type(exc).__name__}: {exc}")

        # 6) 계좌평가잔고
        try:
            summary, holdings = svc.fetch_evaluation()
            line("평가잔고(kt00018)", "OK",
                 f"총평가={_m(summary.get('tot_evlt_amt'))} 손익={_m(summary.get('tot_evlt_pl'))} "
                 f"보유={len(holdings)}종목")
            for h in holdings[:5]:
                line("  └ 보유", "-", f"{h['stk_cd']} {h['stk_nm'][:12]:<12} "
                                      f"{h['rmnd_qty']}주 평가 {_m(h.get('evlt_amt'))} "
                                      f"({h.get('prft_rt')}%)")
        except Exception as exc:  # noqa: BLE001
            ok = False
            line("평가잔고(kt00018)", "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        tokens.revoke()
        rest.close()
        line("토큰 폐기(au10002)", "OK", "사용 후 폐기 완료")

    print("\n".join(out))
    print(f"\n  결과: {'정상' if ok else '일부 실패'}  (주문 API 는 호출하지 않았습니다)\n")
    return 0 if ok else 2


def _safe_api_log(db, api_id, status, rc, msg, elapsed):
    try:
        db.log_api_call(api_id, status, rc, msg, elapsed)
    except Exception:  # noqa: BLE001
        pass


def _m(v) -> str:
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "-"


# ====================================================================== #
def _synthetic_bars(base: int, daily_pct: float, days: int = 20, volume: int = 500_000,
                    last_pct: float | None = None, last_volume: int | None = None,
                    seed: int = 7) -> list[dict]:
    """점검용 합성 일봉. 실서버 조회 없이 만들며, 값이 기계적으로 보이지 않게 흔든다."""
    import datetime as _d
    import random as _r

    rnd = _r.Random(seed)
    bars = []
    price = float(base)
    start = now_kst().date() - _d.timedelta(days=days)
    for i in range(days):
        last = (i == days - 1)
        pct = last_pct if (last and last_pct is not None) else daily_pct + rnd.uniform(-1.2, 1.2)
        vol = last_volume if (last and last_volume is not None) else int(
            volume * rnd.uniform(0.7, 1.4))
        prev = price
        price = max(100.0, round(price * (1 + pct / 100.0), -1))
        bars.append({
            "dt": start + _d.timedelta(days=i),
            "open_pric": int(prev),
            "high_pric": int(max(prev, price) * (1 + rnd.uniform(0.002, 0.015))),
            "low_pric": int(min(prev, price) * (1 - rnd.uniform(0.002, 0.015))),
            "cur_prc": int(price),
            "trde_qty": vol,
        })
    return bars


def _case_from_bars(bars: list[dict]) -> tuple[int, float, int]:
    """합성 일봉과 앞뒤가 맞는 (현재가, 등락률, 거래량)."""
    last, prev = bars[-1], bars[-2]
    cur = int(last["cur_prc"])
    base = int(prev["cur_prc"]) or cur
    return cur, round((cur - base) / base * 100, 2), int(last["trde_qty"])


def cmd_claude_check(cfg) -> int:
    """Claude 거부권 필터 점검. 합성 종목 2건을 실제 API 로 1회씩 검토한다.

    키움 API·주문 코드와 무관하고 DB 에 기록하지 않는다. 키 값은 출력하지 않는다.
    """
    from .algo.claude_advisor import get_client
    from .llm.client import ClaudeError
    from .llm.prompt import build_payload

    print(f"\n=== stock_svr --claude-check ({now_kst():%Y-%m-%d %H:%M:%S} KST) ===")
    print(f"  설정 파일: {cfg.source_path}")
    if not cfg.anthropic.configured:
        print("  [FAIL] [anthropic] apikey_file 경로가 설정되지 않았습니다.")
        return 1
    try:
        cfg.anthropic.read_key()
        print("  [ OK ] API 키 파일 읽기 성공 (값 비표시)")
    except ConfigError as exc:
        print(f"  [FAIL] {exc}")
        return 1

    model = "claude-opus-5"
    normal_bars = _synthetic_bars(45_000, 0.6, volume=600_000, last_pct=1.6,
                                  last_volume=780_000, seed=11)
    risky_bars = _synthetic_bars(8_000, -4.0, volume=300_000, last_pct=29.4,
                                 last_volume=48_000_000, seed=23)
    cases = []
    for title, code, name, bars, reason in (
        ("정상형 · 완만한 상승 추세", "900001", "점검테스트정상", normal_bars,
         "등락률 상위 + 거래량 증가 교집합"),
        ("위험형 · 하락 추세 중 급등/거래량 폭증", "900002", "점검테스트위험", risky_bars,
         "등락률 상위 + 거래량 급증"),
    ):
        cur, flu, qty = _case_from_bars(bars)
        cases.append((title, build_payload(
            now=now_kst(), stk_cd=code, stk_nm=name, cur_prc=cur, flu_rt=flu, trde_qty=qty,
            bars=bars,
            signal={"algo": "momentum_screen", "kind": "entry", "side": "BUY", "score": flu,
                    "reason": reason, "order_type": "시장가"})))

    client = get_client(cfg.anthropic)
    rc = 0
    tot_in = tot_out = 0
    for title, payload in cases:
        print(f"\n  --- {title} ({payload['stock']['code']} {payload['stock']['name']}) ---")
        print(f"      현재가 {payload['stock']['price']:,}원 / 등락률 "
              f"{payload['stock']['change_rate_pct']}% / MA5 {payload['indicators']['ma5']} "
              f"MA20 {payload['indicators']['ma20']}")
        try:
            res = client.review(payload, model=model, effort="low", timeout_sec=60)
        except ClaudeError as exc:
            rc = 2
            print(f"      [FAIL] {exc} (인프라오류={exc.infra})")
            continue
        tot_in += res.input_tokens
        tot_out += res.output_tokens
        print(f"      결정   : {res.decision}   확신도 {res.confidence}")
        print(f"      근거   : {res.reasons_text}")
        print(f"      위험flag: {', '.join(res.risk_flags) or '(없음)'}")
        print(f"      토큰   : 입력 {res.input_tokens:,} / 출력 {res.output_tokens:,}"
              f"   지연 {res.latency_ms:,}ms   모델 {res.model}")
    client.close()
    print(f"\n  합계 토큰: 입력 {tot_in:,} / 출력 {tot_out:,}")
    print("  (DB 에 기록하지 않았고, 키움 API 는 호출하지 않았습니다)\n")
    return rc


# ====================================================================== #
def cmd_eval_once(cfg, db: Database, force_market: bool, all_algos: bool,
                  allow_orders: bool = False) -> int:
    """엔진을 기동해 평가 1회만 수행하고 종료.

    **기본은 관찰 전용(S-09)**: 게이트를 강제로 닫아 어떤 주문도 전송하지 않는다.
    `--allow-orders` 를 준 경우에만 콘솔 확인(`ORDER` 입력)을 거쳐 해제한다.

    R-13: 관찰 전용을 해제해도 **이중 잠금**이 남는다. 이 경로는 엔진을 기동만 하고
    자동거래 스위치를 켜지 않으므로(기동 시 항상 '중지') Executor 가 전송을 막는다.
    즉 `--allow-orders` 는 현재 구조에서 실제로 주문을 내보내지 못한다.
    """
    from .engine.runner import Engine

    observe_only = True
    if allow_orders:
        try:
            answer = input("경고: 이 실행은 실제 주문을 전송할 수 있습니다. "
                           "계속하려면 ORDER 를 입력하세요: ").strip()
        except EOFError:
            answer = ""
        if answer == "ORDER":
            observe_only = False
            log.warning("--allow-orders 확인됨: 관찰 전용 해제 (게이트는 그대로 적용)")
        else:
            print("확인 실패 - 관찰 전용으로 계속합니다.")

    attach_db_handler(db, logging.INFO)
    engine = Engine(cfg, db)
    try:
        engine._startup()  # noqa: SLF001 - 진단 경로에서 기동 단계 재사용
        result = engine.run_cycle(force_market=force_market, all_algos=all_algos,
                                  ignore_auto=True, observe_only=observe_only)
        ctx = result["ctx"]
        print(f"\n=== 평가 사이클 1회 ({ctx.now:%Y-%m-%d %H:%M:%S}) ===")
        print(f"  실행 모드   : {'관찰 전용(주문 전송 불가)' if observe_only else '주문 허용 시도'}")
        print(f"  주문 게이트 : {ctx.gate.describe()}")
        print(f"  장 상태     : {'장중' if ctx.market_open else '장외'}"
              f"{' (강제 평가)' if force_market else ''}")
        print(f"  평가 알고리즘: {', '.join(result['algos']) or '(없음)'}")
        print(f"  생성 신호   : {len(result['signals'])}건 / 전송 {result['sent']}건")
        for res in result["results"]:
            mark = "전송" if res.sent else ("스킵" if res.skipped else "신호기록")
            print(f"   - [{mark}] {res.signal} | {res.blocked_reason or res.status}")
        for note in ctx.notes:
            print(f"  note: {note}")
        return 0
    finally:
        engine._shutdown()  # noqa: SLF001
        time.sleep(0.2)


# ====================================================================== #
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg, db = _prepare(args)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 1

    try:
        if args.claude_check:
            return cmd_claude_check(cfg)
        if args.check:
            return cmd_check(cfg, db)
        if args.eval_once:
            with single_instance():
                return cmd_eval_once(cfg, db, args.force_market, args.all_algos,
                                     args.allow_orders)

        attach_db_handler(db, logging.INFO)
        from .ui.app import run_ui
        log.info("GUI 시작 (smoke=%s)", args.smoke)
        with single_instance():
            return run_ui(cfg, db, autostart=not args.no_autostart, smoke_seconds=args.smoke)
    except AlreadyRunningError as exc:
        print(f"이미 실행 중입니다: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("중단됨")
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
