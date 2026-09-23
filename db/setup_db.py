"""stock_dealings DB 구축 스크립트 (재실행 안전).

  1) schema.sql / seed.sql 적용
  2) 최소권한 DB 계정 생성
       stock_svr : 서버모듈용  (SELECT/INSERT/UPDATE/DELETE)
       stock_web : 웹 조회용   (SELECT + 로그인 관련 최소 쓰기)
  3) 웹 관리자 계정 생성(비밀번호는 PHP password_hash 로 해시 저장)
  4) 계정 비밀번호를 gitignore 대상 로컬 설정파일에 기록
       server/config/config.local.ini , web/config/config.local.php

비밀값은 환경변수로만 받는다(명령행/파일에 남기지 않기 위해):
  STOCK_DB_ROOT_PW   MariaDB root 비밀번호 (필수)
  STOCK_ADMIN_USER   웹 관리자 ID          (선택)
  STOCK_ADMIN_PW     웹 관리자 비밀번호    (선택, 있으면 관리자 계정 생성/갱신)
선택 환경변수: STOCK_DB_HOST(127.0.0.1) STOCK_DB_PORT(4406) STOCK_DB_NAME(stock_dealings)
"""
from __future__ import annotations

import os
import re
import secrets
import string
import subprocess
import sys
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parent.parent
HOST = os.environ.get("STOCK_DB_HOST", "127.0.0.1")
PORT = int(os.environ.get("STOCK_DB_PORT", "4406"))
DBNAME = os.environ.get("STOCK_DB_NAME", "stock_dealings")
PHP = os.environ.get("STOCK_PHP", r"D:\xampp\php\php.exe")

SERVER_CFG = ROOT / "server" / "config" / "config.local.ini"
WEB_CFG = ROOT / "web" / "config" / "config.local.php"

WEB_TABLES_SELECT = None  # 전 테이블 SELECT (아래에서 조회)


def rand_pw(n: int = 28) -> str:
    # ini(configparser 보간)/PHP 단일따옴표 문자열에서 안전한 문자만 사용
    alphabet = string.ascii_letters + string.digits + "-_.^*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(n))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)):
            return pw


def split_sql(text: str) -> list[str]:
    """세미콜론 기준 분리 (주석/문자열 내 세미콜론은 없다는 전제 - 본 프로젝트 SQL 한정)."""
    lines = []
    for ln in text.splitlines():
        if ln.strip().startswith("--"):
            continue
        lines.append(ln)
    stmts = [s.strip() for s in "\n".join(lines).split(";\n")]
    return [s.rstrip(";").strip() for s in stmts if s.strip().rstrip(";").strip()]


def run_file(cur, path: Path) -> int:
    n = 0
    for stmt in split_sql(path.read_text(encoding="utf-8")):
        cur.execute(stmt)
        n += 1
    return n


def php_hash(password: str) -> str:
    env = dict(os.environ, STOCK_HASH_INPUT=password)
    # -n: php.ini 없이 실행 (xampp 확장 로딩 경고 회피). password_hash 는 코어 함수.
    out = subprocess.run(
        [PHP, "-n", "-r", "echo password_hash(getenv('STOCK_HASH_INPUT'), PASSWORD_DEFAULT);"],
        env=env, capture_output=True, check=True)
    h = out.stdout.decode("ascii", "ignore").strip()
    if not h.startswith("$"):
        raise RuntimeError("PHP password_hash 실패")
    return h


def read_existing_ini_pw(path: Path, section: str) -> str | None:
    if not path.exists():
        return None
    # 줄 단위로 해당 섹션의 password 한 줄만 읽는다.
    # (이전 구현은 re.S 정규식이 파일 나머지 전체를 비밀번호로 잡아 설정파일이 중복되고
    #  DB 계정 비밀번호가 여러 줄짜리 값으로 바뀌는 버그가 있었다)
    cur = None
    for ln in path.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
            continue
        if cur == section:
            m = re.match(r"password\s*=\s*(\S.*)$", s)
            if m:
                pw = m.group(1).strip()
                return pw if pw and "\n" not in pw else None
    return None


def main() -> int:
    root_pw = os.environ.get("STOCK_DB_ROOT_PW")
    if not root_pw:
        print("STOCK_DB_ROOT_PW 환경변수가 필요합니다.", file=sys.stderr)
        return 2

    conn = pymysql.connect(host=HOST, port=PORT, user="root", password=root_pw,
                           charset="utf8mb4", autocommit=True)
    cur = conn.cursor()
    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DBNAME}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cur.execute(f"USE `{DBNAME}`")

    n1 = run_file(cur, ROOT / "db" / "schema.sql")
    n2 = run_file(cur, ROOT / "db" / "seed.sql")
    print(f"schema {n1}문, seed {n2}문 적용")

    # ---- DB 계정 ------------------------------------------------------
    svr_pw = os.environ.get("STOCK_SVR_DB_PW") or read_existing_ini_pw(SERVER_CFG, "db") or rand_pw()
    web_pw = os.environ.get("STOCK_WEB_DB_PW") or None
    if web_pw is None and WEB_CFG.exists():
        m = re.search(r"'password'\s*=>\s*'([^']+)'", WEB_CFG.read_text(encoding="utf-8"))
        web_pw = m.group(1) if m else None
    web_pw = web_pw or rand_pw()

    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (DBNAME,))
    tables = [r[0] for r in cur.fetchall()]

    for host in ("127.0.0.1", "localhost"):
        for user, pw in (("stock_svr", svr_pw), ("stock_web", web_pw)):
            cur.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s", (user, host, pw))
            cur.execute("ALTER USER %s@%s IDENTIFIED BY %s", (user, host, pw))
            cur.execute("REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@%s", (user, host))
        cur.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{DBNAME}`.* TO 'stock_svr'@'{host}'")
        for t in tables:
            cur.execute(f"GRANT SELECT ON `{DBNAME}`.`{t}` TO 'stock_web'@'{host}'")
        cur.execute(f"GRANT INSERT ON `{DBNAME}`.`app_login_log` TO 'stock_web'@'{host}'")
        cur.execute(f"GRANT INSERT ON `{DBNAME}`.`trend_scan_request` TO 'stock_web'@'{host}'")
        cur.execute(f"GRANT UPDATE (failed_count, locked_until, last_login_at, password_hash) "
                    f"ON `{DBNAME}`.`app_user` TO 'stock_web'@'{host}'")
    cur.execute("FLUSH PRIVILEGES")
    print("DB 계정 stock_svr / stock_web 생성·권한 부여 완료")

    # ---- 로컬 설정파일 기록 (비밀값 - git 제외) ------------------------
    SERVER_CFG.parent.mkdir(parents=True, exist_ok=True)
    WEB_CFG.parent.mkdir(parents=True, exist_ok=True)
    if not SERVER_CFG.exists():
        ex = ROOT / "server" / "config" / "config.example.ini"
        SERVER_CFG.write_text(ex.read_text(encoding="utf-8"), encoding="utf-8")
    out_lines, section = [], None
    for ln in SERVER_CFG.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
        elif section == "db" and re.match(r"password\s*=", s):
            ln = "password = " + svr_pw
        out_lines.append(ln)
    SERVER_CFG.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    WEB_CFG.write_text(
        "<?php\n// 자동 생성 - 비밀값 포함, git 커밋 금지 (gitignore 대상)\nreturn [\n"
        "  'db' => [\n"
        f"    'host' => '{HOST}',\n    'port' => {PORT},\n    'name' => '{DBNAME}',\n"
        f"    'user' => 'stock_web',\n    'password' => '{web_pw}',\n  ],\n];\n",
        encoding="utf-8")
    print(f"설정파일 기록: {SERVER_CFG.name}, {WEB_CFG.name}")

    # ---- 웹 관리자 -----------------------------------------------------
    admin_user, admin_pw = os.environ.get("STOCK_ADMIN_USER"), os.environ.get("STOCK_ADMIN_PW")
    if admin_user and admin_pw:
        h = php_hash(admin_pw)
        cur.execute(
            "INSERT INTO app_user (username, password_hash, display_name, role) VALUES (%s,%s,%s,'admin') "
            "ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), role='admin', is_active=1, "
            "failed_count=0, locked_until=NULL",
            (admin_user, h, admin_user))
        print(f"웹 관리자 '{admin_user}' 생성/갱신 (비밀번호는 해시로만 저장)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
