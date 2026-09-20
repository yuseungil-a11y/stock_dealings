"""실제 DB(information_schema)에서 docs/table_design.md 를 생성한다 (설계서-DB 불일치 방지).

사용: python tools/gen_table_doc.py      (서버 config.local.ini 의 [db] 접속정보 사용)
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent

GROUPS = [
    ("웹 사용자·로그인", ["app_user", "app_login_log"]),
    ("계좌·잔고·보유종목", ["account", "account_balance", "holding", "holding_snapshot", "position_state"]),
    ("종목·시세", ["stock_master", "price_daily", "screening_result"]),
    ("주문·체결·거래내역", ["algo_run", "orders", "executions", "trade_ledger", "daily_trade_summary"]),
    ("알고리즘·파라미터", ["algorithm", "algorithm_param_def", "algorithm_param_value",
                     "algorithm_param_history", "algorithm_selection", "signal_log"]),
    ("시스템·관제", ["system_setting", "server_status", "event_log", "api_call_log"]),
    ("거래 분석 기록(영구 보관)", ["order_event", "event_archive", "api_error_log"]),
]


def main() -> None:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(ROOT / "server" / "config" / "config.local.ini", encoding="utf-8")
    d = cfg["db"]
    conn = pymysql.connect(host=d["host"], port=int(d["port"]), user=d["user"],
                           password=d["password"], database=d["name"], charset="utf8mb4")
    cur = conn.cursor()
    cur.execute("SELECT table_name, table_comment, table_type FROM information_schema.tables "
                "WHERE table_schema=%s", (d["name"],))
    rows_t = cur.fetchall()
    tcomment = {r[0]: r[1] for r in rows_t}
    # 뷰(v_trade_analysis 등)는 테이블 목록과 분리해 맨 뒤에 따로 싣는다
    views = sorted(r[0] for r in rows_t if str(r[2] or "").upper() == "VIEW")
    cur.execute("""SELECT table_name, column_name, column_type, is_nullable, column_key, column_default, column_comment
                   FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name, ordinal_position""", (d["name"],))
    cols: dict[str, list] = {}
    for r in cur.fetchall():
        cols.setdefault(r[0], []).append(r[1:])
    table_count = len([t for t in cols if t not in views])

    out = ["# 테이블 설계서 — stock_dealings", "",
           "> `tools/gen_table_doc.py` 가 실제 DB 에서 자동 생성한 문서입니다(직접 수정 금지). 원본 DDL: `db/schema.sql`, 초기 데이터: `db/seed.sql`.", "",
           f"- DBMS: MariaDB 10.11, 문자셋 utf8mb4, 총 **{table_count}개 테이블**"
           + (f" + 뷰 {len(views)}개" if views else ""),
           "- 금액/수량/가격 = BIGINT, 비율(%) = DECIMAL(12,4), 시각 = DATETIME(KST)",
           "- 키움 API 문자열 값(부호·0패딩)은 서버모듈이 정수로 파싱해 저장",
           "- 권한: `stock_svr`(서버, SELECT/INSERT/UPDATE/DELETE) · `stock_web`(웹, SELECT 전용 + `app_login_log` INSERT + `app_user` 일부 컬럼 UPDATE)", ""]
    listed = set()
    for gname, tables in GROUPS:
        out += [f"## {gname}", ""]
        for t in tables:
            if t not in cols:
                continue
            listed.add(t)
            out += [f"### `{t}`", "", f"{tcomment.get(t, '')}", "",
                    "| 컬럼 | 타입 | NULL | 키 | 기본값 | 설명 |", "|---|---|---|---|---|---|"]
            for (c, ty, nl, key, df, cm) in cols[t]:
                out.append(f"| `{c}` | {ty} | {'Y' if nl == 'YES' else 'N'} | {key or ''} | {'' if df is None else df} | {(cm or '').replace('|', '/')} |")
            out.append("")
    rest = sorted(set(cols) - listed - set(views))
    if rest:
        out += ["## 기타", ""] + [f"- `{t}` — {tcomment.get(t, '')}" for t in rest] + [""]
    if views:
        out += ["## 분석용 뷰", "",
                "> 읽기 전용입니다. 서버 코드는 뷰에 쓰지 않습니다.", ""]
        for v in views:
            out += [f"### `{v}`", "",
                    "| 컬럼 | 타입 | NULL | 설명 |", "|---|---|---|---|"]
            for (c, ty, nl, _key, _df, cm) in cols.get(v, []):
                out.append(f"| `{c}` | {ty} | {'Y' if nl == 'YES' else 'N'} | "
                           f"{(cm or '').replace('|', '/')} |")
            out.append("")
    (ROOT / "docs" / "table_design.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"docs/table_design.md 생성: {table_count}개 테이블, 뷰 {len(views)}개")


if __name__ == "__main__":
    main()
