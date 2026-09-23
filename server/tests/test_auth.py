"""자동거래 시작 확인창의 아이디/비밀번호 재확인(app_user, 웹과 공유) 회귀 테스트.

실 DB 연결 없이 `Database.query_one` 만 흉내낸 대역(AuthSpyDb, test_audit_fixes.py 의
SqlSpyDb 와 같은 패턴)으로 `Database.verify_login()` 자체 로직(bcrypt 검증·is_active·
locked_until 판단)을 검증한다. bcrypt 해시 검증 자체는 DB 접근이 필요 없는 순수함수
`_check_password()` 로 분리돼 있어 별도로도 직접 검증한다.
"""
from __future__ import annotations

import datetime as _dt

import bcrypt
import pytest

from stock_svr.config import DbConfig
from stock_svr.db import Database, _DUMMY_PASSWORD_HASH, _check_password
from stock_svr.util import now_kst


def bcrypt_hash(password: str, php_style: bool = False) -> str:
    """테스트용 bcrypt 해시. `php_style=True` 면 PHP password_hash() 와 같은 `$2y$` 접두사."""
    h = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    if php_style:
        h = h.replace("$2b$", "$2y$", 1)
    return h


class AuthSpyDb(Database):
    """`query_one` 만 app_user 테이블처럼 흉내내는 Database 대역(실 DB 연결 없음)."""

    def __init__(self, rows: dict[str, dict]):
        Database.__init__(self, DbConfig())
        self.rows = rows

    def query_one(self, sql, args=None):
        if "app_user" in sql and args:
            row = self.rows.get(args[0])
            return dict(row) if row else None
        return None


def user_row(**over) -> dict:
    row = {"id": 1, "username": "admin1", "password_hash": bcrypt_hash("Secret123!"),
           "display_name": "관리자", "role": "admin", "is_active": 1, "locked_until": None}
    row.update(over)
    return row


# ====================================================================== #
# _check_password (순수함수, DB 접근 없음)
# ====================================================================== #
def test_check_password_verifies_2b_hash():
    h = bcrypt_hash("hello")
    assert _check_password(h, "hello") is True
    assert _check_password(h, "wrong") is False


def test_check_password_verifies_php_style_2y_hash():
    """PHP password_hash() 가 만드는 `$2y$` 해시도 (치환 로직으로) 검증돼야 한다."""
    h = bcrypt_hash("hello", php_style=True)
    assert h.startswith("$2y$")
    assert _check_password(h, "hello") is True
    assert _check_password(h, "wrong") is False


def test_check_password_rejects_empty_or_none_hash():
    assert _check_password(None, "x") is False
    assert _check_password("", "x") is False


def test_check_password_rejects_malformed_hash():
    assert _check_password("not-a-bcrypt-hash", "x") is False


# ====================================================================== #
# Database.verify_login
# ====================================================================== #
def test_verify_login_success_returns_role():
    db = AuthSpyDb({"admin1": user_row()})
    ok, role = db.verify_login("admin1", "Secret123!")
    assert ok is True and role == "admin"


def test_verify_login_wrong_password():
    db = AuthSpyDb({"admin1": user_row()})
    ok, reason = db.verify_login("admin1", "wrong-password")
    assert ok is False and reason == "비밀번호 불일치"


def test_verify_login_unknown_account_still_runs_bcrypt(monkeypatch):
    """계정이 없어도 더미 해시로 bcrypt 검증이 실제로 수행돼야 한다(타이밍 사이드채널 방지)."""
    calls: list[bytes] = []
    real_checkpw = bcrypt.checkpw

    def spy_checkpw(pw, hashed):
        calls.append(hashed)
        return real_checkpw(pw, hashed)

    monkeypatch.setattr("stock_svr.db.bcrypt.checkpw", spy_checkpw)
    db = AuthSpyDb({})
    ok, reason = db.verify_login("no-such-user", "whatever")
    assert ok is False and reason == "계정 없음"
    assert calls, "bcrypt.checkpw 가 호출되지 않음(더미 해시 검증 경로를 타지 않음)"
    assert calls[0].decode() == _DUMMY_PASSWORD_HASH.replace("$2y$", "$2b$", 1)


def test_verify_login_empty_username_returns_account_missing():
    db = AuthSpyDb({})
    ok, reason = db.verify_login("", "whatever")
    assert ok is False and reason == "계정 없음"


def test_verify_login_inactive_account():
    db = AuthSpyDb({"admin1": user_row(is_active=0)})
    ok, reason = db.verify_login("admin1", "Secret123!")
    assert ok is False and reason == "비활성 계정"


def test_verify_login_locked_account_in_future_is_rejected():
    db = AuthSpyDb({"admin1": user_row(locked_until=now_kst() + _dt.timedelta(hours=1))})
    ok, reason = db.verify_login("admin1", "Secret123!")
    assert ok is False and reason == "잠긴 계정"


def test_verify_login_locked_until_in_past_is_allowed():
    db = AuthSpyDb({"admin1": user_row(locked_until=now_kst() - _dt.timedelta(hours=1))})
    ok, role = db.verify_login("admin1", "Secret123!")
    assert ok is True and role == "admin"


def test_verify_login_php_style_2y_hash_from_real_account():
    """실제 PHP password_hash() 스타일 `$2y$` 해시(웹과 공유하는 실제 형식)로도 검증돼야 한다."""
    db = AuthSpyDb({"admin1": user_row(password_hash=bcrypt_hash("Secret123!", php_style=True))})
    ok, role = db.verify_login("admin1", "Secret123!")
    assert ok is True and role == "admin"


def test_verify_login_viewer_role_is_returned_as_is():
    """role 판단(admin 만 허용)은 호출부(다이얼로그) 책임 - verify_login 은 role 을 그대로 돌려준다."""
    db = AuthSpyDb({"viewer1": user_row(username="viewer1", role="viewer")})
    ok, role = db.verify_login("viewer1", "Secret123!")
    assert ok is True and role == "viewer"


@pytest.mark.parametrize("bad_pw", ["", "Secret123", "secret123!"])
def test_verify_login_rejects_close_but_wrong_passwords(bad_pw):
    db = AuthSpyDb({"admin1": user_row()})
    ok, reason = db.verify_login("admin1", bad_pw)
    assert ok is False and reason == "비밀번호 불일치"
