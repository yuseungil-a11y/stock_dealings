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


class FakeRun:
    """`subprocess.run` 대역(icacls). 실제 프로세스를 띄우지 않고 호출 인자만 기록한다."""

    def __init__(self, returncode=0, exc=None):
        self.calls = []
        self.returncode = returncode
        self.exc = exc

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc

        class _Proc:
            pass

        p = _Proc()
        p.returncode = self.returncode
        p.stdout = b""
        p.stderr = b""
        return p


@pytest.fixture(autouse=True)
def fake_run(monkeypatch):
    """모든 테스트에서 icacls 실제 실행을 막는다(테스트용 임시파일 ACL 을 건드리지 않게)."""
    fr = FakeRun()
    monkeypatch.setattr(mail_notify.subprocess, "run", fr)
    monkeypatch.setenv("USERNAME", "testuser")
    return fr


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


# ====================================================================== #
# save_mail_config (설정 탭 저장 - 검증 실패는 ValueError 로 드러낸다)
# ====================================================================== #
def test_save_mail_config_writes_pretty_json_and_roundtrips(tmp_path):
    path = tmp_path / "sub" / "mail.local.json"      # 상위 폴더가 없어도 만들어야 한다
    mail_notify.save_mail_config(VALID_CFG, path)
    text = path.read_text(encoding="utf-8")
    assert "주식 자동매매 알림" in text               # ensure_ascii=False
    assert "\n  " in text                              # indent=2
    assert json.loads(text) == VALID_CFG
    assert mail_notify.load_mail_config(path) == VALID_CFG


@pytest.mark.parametrize("missing_key, label", [
    ("host", "SMTP 호스트"),
    ("port", "포트"),
    ("user", "발신 계정 아이디"),
    ("password", "발신 계정 비밀번호"),
    ("from_addr", "발신 주소"),
])
def test_save_mail_config_rejects_missing_smtp_field(tmp_path, missing_key, label):
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["smtp"][missing_key] = ""
    path = tmp_path / "mail.local.json"
    with pytest.raises(ValueError) as ei:
        mail_notify.save_mail_config(cfg, path)
    assert label in str(ei.value)
    assert not path.exists()


def test_save_mail_config_rejects_missing_to_addr(tmp_path):
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["to_addr"] = ""
    with pytest.raises(ValueError, match="수신 주소"):
        mail_notify.save_mail_config(cfg, tmp_path / "mail.local.json")


@pytest.mark.parametrize("port", ["abc", 0, 70000])
def test_save_mail_config_rejects_bad_port(tmp_path, port):
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["smtp"]["port"] = port
    with pytest.raises(ValueError, match="포트"):
        mail_notify.save_mail_config(cfg, tmp_path / "mail.local.json")


def test_save_mail_config_rejects_non_dict(tmp_path):
    with pytest.raises(ValueError):
        mail_notify.save_mail_config({"to_addr": "a@b.com"}, tmp_path / "m.json")


def test_save_mail_config_error_never_contains_password(tmp_path):
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["smtp"]["password"] = "TOPSECRET-PW"
    cfg["smtp"]["host"] = ""
    with pytest.raises(ValueError) as ei:
        mail_notify.save_mail_config(cfg, tmp_path / "m.json")
    assert "TOPSECRET-PW" not in str(ei.value)


# ====================================================================== #
# save_mail_config - 저장 후 icacls 로 접근권한 제한 (매 저장마다, 실패해도 저장은 유지)
# ====================================================================== #
def test_save_mail_config_restricts_acl_with_icacls(tmp_path, fake_run):
    path = tmp_path / "mail.local.json"
    warning = mail_notify.save_mail_config(VALID_CFG, path)
    assert warning is None
    assert len(fake_run.calls) == 1
    args, kwargs = fake_run.calls[0]
    assert args == ["icacls", str(path), "/inheritance:r", "/grant:r",
                    "testuser:(F)", "BUILTIN\\Administrators:(F)", "NT AUTHORITY\\SYSTEM:(F)"]
    assert kwargs.get("check") is False
    # 비밀번호가 명령 인자에 절대 들어가지 않는다
    assert all("secret" not in a for a in args)


def test_save_mail_config_reapplies_acl_on_every_save(tmp_path, fake_run):
    path = tmp_path / "mail.local.json"
    mail_notify.save_mail_config(VALID_CFG, path)
    mail_notify.save_mail_config(VALID_CFG, path)
    assert len(fake_run.calls) == 2


def test_save_mail_config_no_icacls_when_validation_fails(tmp_path, fake_run):
    cfg = json.loads(json.dumps(VALID_CFG))
    cfg["to_addr"] = ""
    with pytest.raises(ValueError):
        mail_notify.save_mail_config(cfg, tmp_path / "mail.local.json")
    assert fake_run.calls == []


def test_save_mail_config_icacls_exception_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_notify.subprocess, "run",
                        FakeRun(exc=FileNotFoundError("icacls not found")))
    path = tmp_path / "mail.local.json"
    warning = mail_notify.save_mail_config(VALID_CFG, path)   # 예외가 올라오면 안 된다
    assert warning and "icacls" in warning
    assert "secret" not in warning
    assert mail_notify.load_mail_config(path) == VALID_CFG     # JSON 저장 자체는 성공


def test_save_mail_config_icacls_nonzero_exit_returns_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_notify.subprocess, "run", FakeRun(returncode=5))
    path = tmp_path / "mail.local.json"
    warning = mail_notify.save_mail_config(VALID_CFG, path)
    assert warning and "5" in warning
    assert mail_notify.load_mail_config(path) == VALID_CFG


def test_save_mail_config_without_username_warns_and_skips_icacls(tmp_path, monkeypatch, fake_run):
    monkeypatch.delenv("USERNAME", raising=False)
    path = tmp_path / "mail.local.json"
    warning = mail_notify.save_mail_config(VALID_CFG, path)
    assert warning
    assert fake_run.calls == []
    assert mail_notify.load_mail_config(path) == VALID_CFG


# ====================================================================== #
# disable_mail_config ([메일 알림 끄기] - 파일 삭제, 예외 없음)
# ====================================================================== #
def test_disable_mail_config_deletes_existing_file(tmp_path):
    path = tmp_path / "mail.local.json"
    mail_notify.save_mail_config(VALID_CFG, path)
    assert path.exists()
    assert mail_notify.disable_mail_config(path) is True
    assert not path.exists()
    assert mail_notify.load_mail_config(path) is None      # 엔진 핫리로드 결과도 None(꺼짐)


def test_disable_mail_config_missing_file_is_safe_noop(tmp_path):
    assert mail_notify.disable_mail_config(tmp_path / "nope.json") is False


def test_disable_mail_config_os_error_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "mail.local.json"
    path.write_text("{}", encoding="utf-8")

    def boom(self, *a, **kw):
        raise PermissionError("locked")

    monkeypatch.setattr(mail_notify.Path, "unlink", boom)
    assert mail_notify.disable_mail_config(path) is False


# ====================================================================== #
# send_test_mail (동기, 결과 튜플 반환, 예외 절대 안 새어나감)
# ====================================================================== #
def test_send_test_mail_success(monkeypatch):
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", FakeSmtp)
    ok, msg = mail_notify.send_test_mail(VALID_CFG)
    assert ok is True
    assert msg == "발송 성공"
    smtp = FakeSmtp.instances[0]
    assert smtp.started_tls is True
    assert smtp.logged_in == ("pms@utinfo.co.kr", "secret")
    sent = smtp.sent[0]
    assert "테스트" in sent["Subject"]
    assert "실제 체결 아님" in sent["Subject"]
    assert sent["To"] == "siy@utinfo.co.kr"


def test_send_test_mail_login_failure_returns_false(monkeypatch):
    def make_failing(*args, **kwargs):
        smtp = FakeSmtp(*args, **kwargs)
        smtp.login_exc = RuntimeError("auth failed")
        return smtp

    monkeypatch.setattr(mail_notify.smtplib, "SMTP", make_failing)
    ok, msg = mail_notify.send_test_mail(VALID_CFG)
    assert ok is False
    assert "auth failed" in msg
    assert FakeSmtp.instances[0].sent == []


def test_send_test_mail_connect_failure_returns_false(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(mail_notify.smtplib, "SMTP", boom)
    ok, msg = mail_notify.send_test_mail(VALID_CFG)
    assert ok is False
    assert "connection refused" in msg


def test_send_test_mail_masks_password_in_error(monkeypatch):
    def make_failing(*args, **kwargs):
        smtp = FakeSmtp(*args, **kwargs)
        smtp.login_exc = RuntimeError("bad credentials for secret")
        return smtp

    monkeypatch.setattr(mail_notify.smtplib, "SMTP", make_failing)
    ok, msg = mail_notify.send_test_mail(VALID_CFG)
    assert ok is False
    assert "secret" not in msg


def test_send_test_mail_incomplete_cfg_does_not_connect(monkeypatch):
    called = []
    monkeypatch.setattr(mail_notify.smtplib, "SMTP", lambda *a, **kw: called.append(1))
    ok, msg = mail_notify.send_test_mail({"smtp": {"host": "x"}, "to_addr": ""})
    assert ok is False
    assert msg
    assert called == []
