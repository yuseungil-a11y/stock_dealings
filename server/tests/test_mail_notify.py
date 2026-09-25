"""체결 완료 알림 메일(mail_notify) 테스트.

실제 SMTP 서버로는 절대 나가지 않는다 - `smtplib.SMTP` 를 항상 가짜로 바꿔서 검증한다.
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from stock_svr.services import mail_notify

VALID_CFG = {
    "smtp": {
        "host": "smtps.hiworks.com",
        "port": 587,
        "user": "pms@utinfo.co.kr",
        "password": "secret",
        "from_addr": "pms@utinfo.co.kr",
        "from_name": "주식 자동매매 알림",
    },
    "to_addr": "siy@utinfo.co.kr",
}


class FakeSmtp:
    """`smtplib.SMTP` 대역. 실제 소켓을 전혀 열지 않는다."""

    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        self.login_exc = None
        FakeSmtp.instances.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        if self.login_exc:
            raise self.login_exc
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSmtp.instances = []
    yield
    FakeSmtp.instances = []


# ====================================================================== #
# load_mail_config
# ====================================================================== #
def test_load_mail_config_missing_file_returns_none(tmp_path):
    assert mail_notify.load_mail_config(tmp_path / "nope.json") is None


def test_load_mail_config_invalid_json_returns_none(tmp_path):
    bad = tmp_path / "mail.local.json"
    bad.write_text("{ 이건 json 아님", encoding="utf-8")
    assert mail_notify.load_mail_config(bad) is None


def test_load_mail_config_valid_json(tmp_path):
    path = tmp_path / "mail.local.json"
    path.write_text(json.dumps(VALID_CFG, ensure_ascii=False), encoding="utf-8")
    cfg = mail_notify.load_mail_config(path)
    assert cfg == VALID_CFG


# ====================================================================== #
# send_trade_completed_mail - cfg 없음/불완전
# ====================================================================== #
def test_send_returns_immediately_when_cfg_is_none(monkeypatch):
    called = []
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", lambda *a, **kw: called.append(1))
    mail_notify.send_trade_completed_mail(
        None, side="BUY", stk_cd="005930", stk_nm="삼성전자", qty=10, price=60000,
        executed_at=_dt.datetime(2026, 9, 25, 10, 0, 0))
    assert called == []


def test_send_noop_when_smtp_fields_incomplete(monkeypatch):
    called = []
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", lambda *a, **kw: called.append(1))
    incomplete = {"smtp": {"host": "x"}, "to_addr": "a@b.com"}
    mail_notify.send_trade_completed_mail(
        incomplete, side="BUY", stk_cd="005930", stk_nm="삼성전자", qty=10, price=60000,
        executed_at=_dt.datetime(2026, 9, 25, 10, 0, 0))
    assert called == []


# ====================================================================== #
# send_trade_completed_mail - 정상 발송 (동기 core 함수 직접 호출)
# ====================================================================== #
def test_send_sync_calls_smtp_correctly(monkeypatch):
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", FakeSmtp)
    mail_notify._send_sync(
        VALID_CFG, side="SELL", stk_cd="005930", stk_nm="삼성전자", qty=5, price=61000,
        executed_at=_dt.datetime(2026, 9, 25, 9, 30, 15))

    assert len(FakeSmtp.instances) == 1
    smtp = FakeSmtp.instances[0]
    assert smtp.host == "smtps.hiworks.com"
    assert smtp.port == 587
    assert smtp.started_tls is True
    assert smtp.logged_in == ("pms@utinfo.co.kr", "secret")
    assert len(smtp.sent) == 1
    msg = smtp.sent[0]
    assert msg["To"] == "siy@utinfo.co.kr"
    assert "매도" in msg["Subject"]
    assert "삼성전자" in msg["Subject"]
    assert "005930" in msg["Subject"]


def test_send_trade_completed_mail_runs_in_background_thread(monkeypatch):
    """threading 래퍼도 스레드 join 후 같은 결과가 나오는지 확인."""
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", FakeSmtp)
    captured_threads = []
    real_thread = mail_notify.threading.Thread

    def spy_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        captured_threads.append(t)
        return t

    monkeypatch.setattr(mail_notify.threading, "Thread", spy_thread)
    mail_notify.send_trade_completed_mail(
        VALID_CFG, side="BUY", stk_cd="005930", stk_nm="삼성전자", qty=10, price=60000,
        executed_at=_dt.datetime(2026, 9, 25, 10, 0, 0))
    assert len(captured_threads) == 1
    captured_threads[0].join(timeout=5)
    assert len(FakeSmtp.instances) == 1
    assert len(FakeSmtp.instances[0].sent) == 1


# ====================================================================== #
# 발송 실패는 절대 예외를 밖으로 던지지 않는다
# ====================================================================== #
def test_send_sync_swallows_login_failure(monkeypatch):
    def make_failing(*args, **kwargs):
        smtp = FakeSmtp(*args, **kwargs)
        smtp.login_exc = RuntimeError("auth failed")
        return smtp

    monkeypatch.setattr(mail_notify.smtplib, "SMTP", make_failing)
    # 예외가 전혀 올라오지 않아야 한다.
    mail_notify._send_sync(
        VALID_CFG, side="BUY", stk_cd="005930", stk_nm="삼성전자", qty=10, price=60000,
        executed_at=_dt.datetime(2026, 9, 25, 10, 0, 0))
    assert FakeSmtp.instances[0].sent == []


def test_send_trade_completed_mail_thread_swallows_exception(monkeypatch):
    """스레드로 감싼 경로에서도 로그인 실패가 밖으로 새지 않는지 join 해서 확인."""
    def make_failing(*args, **kwargs):
        smtp = FakeSmtp(*args, **kwargs)
        smtp.login_exc = RuntimeError("auth failed")
        return smtp

    monkeypatch.setattr(mail_notify.smtplib, "SMTP", make_failing)
    real_thread = mail_notify.threading.Thread
    captured_threads = []

    def spy_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        captured_threads.append(t)
        return t

    monkeypatch.setattr(mail_notify.threading, "Thread", spy_thread)
    mail_notify.send_trade_completed_mail(
        VALID_CFG, side="BUY", stk_cd="005930", stk_nm="삼성전자", qty=10, price=60000,
        executed_at=_dt.datetime(2026, 9, 25, 10, 0, 0))
    captured_threads[0].join(timeout=5)
    # 예외 없이 종료되면 성공(스레드가 살아있으면 join 이 timeout 없이 끝난다).
    assert not captured_threads[0].is_alive()
