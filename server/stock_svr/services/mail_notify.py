"""체결(거래) 완료 시 지정 수신자에게 이메일로 알린다 — **참고용 알림, 매매 판단에는 쓰이지 않는다.**

설정은 코드에 값을 두지 않고 실행경로의 `config/mail.local.json`(git 비추적)에서 읽는다
(`config.local.ini` 와 같은 위치 원칙, 사용자 요청에 따라 JSON 파일로 분리).
파일이 없거나 값이 부족하면 조용히 비활성화된다 - 다른 선택적 통합(DART/Anthropic)과
같은 fail-soft 철학이며, 메일 실패가 주문/체결 동기화를 절대 막지 않는다.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import smtplib
import subprocess
import threading
from email.message import EmailMessage
from pathlib import Path

from ..config import SERVER_DIR

log = logging.getLogger(__name__)

MAIL_CONFIG_PATH = SERVER_DIR / "config" / "mail.local.json"

_SIDE_LABEL = {"BUY": "매수", "SELL": "매도"}

_REQUIRED_SMTP_KEYS = ("host", "port", "user", "password", "from_addr")


def load_mail_config(path: str | Path | None = None) -> dict | None:
    """`mail.local.json` 을 읽는다.

    파일이 없거나 JSON 형식이 잘못되면 경고 로그만 남기고 `None` 을 돌려준다
    (예외를 절대 던지지 않는다).
    """
    cfg_path = Path(path) if path else MAIL_CONFIG_PATH
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError:
        log.warning("메일 설정 파일이 없습니다: %s (체결 알림 메일 비활성화)", cfg_path)
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.warning("메일 설정 파일 형식(JSON) 오류: %s (체결 알림 메일 비활성화)", cfg_path)
        return None
    if not isinstance(data, dict):
        log.warning("메일 설정 파일 형식 오류(object 아님): %s", cfg_path)
        return None
    return data


def _is_valid(cfg: dict | None) -> bool:
    if not cfg:
        return False
    smtp = cfg.get("smtp")
    if not isinstance(smtp, dict):
        return False
    if not all(smtp.get(k) for k in _REQUIRED_SMTP_KEYS):
        return False
    return bool(cfg.get("to_addr"))


_FIELD_LABEL = {
    "host": "SMTP 호스트",
    "port": "포트",
    "user": "발신 계정 아이디",
    "password": "발신 계정 비밀번호",
    "from_addr": "발신 주소",
}


def _restrict_acl(cfg_path: Path) -> str | None:
    """`cfg_path` 의 접근권한을 현재 사용자/Administrators/SYSTEM 전용으로 제한한다.

    `tools/build_server.ps1` 의 복사 단계와 동일한 icacls 명령이다
    (`/inheritance:r /grant:r <USERNAME>:(F) BUILTIN\\Administrators:(F) NT AUTHORITY\\SYSTEM:(F)`).
    파일에 SMTP 비밀번호가 들어 있으므로 같은 PC 의 다른 계정이 읽지 못하게 하는 보조 방어책이다.
    매 저장마다 다시 적용한다(이미 제한된 파일에 재적용해도 결과가 같다 - 멱등).
    성공하면 `None`, 실패하면 화면에 보여줄 경고 문구를 돌려준다. 예외를 던지지 않는다.
    """
    username = os.environ.get("USERNAME")
    if not username:
        log.warning("메일 설정 파일 권한 제한 생략: USERNAME 환경변수 없음 (%s)", cfg_path)
        return "USERNAME 환경변수를 찾지 못해 파일 접근권한을 제한하지 못했습니다."
    try:
        proc = subprocess.run(
            ["icacls", str(cfg_path), "/inheritance:r", "/grant:r",
             f"{username}:(F)", "BUILTIN\\Administrators:(F)", "NT AUTHORITY\\SYSTEM:(F)"],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=False,
            # 콘솔 없는(PyInstaller windowed) 앱에서 검은 창이 번쩍이지 않게
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:  # noqa: BLE001 - 권한 제한 실패가 저장 자체를 막지 않게
        log.warning("메일 설정 파일 권한 제한(icacls) 실패: %s (%s)", type(exc).__name__, cfg_path)
        return f"파일 접근권한 제한(icacls) 실패: {type(exc).__name__}"
    if proc.returncode != 0:
        log.warning("메일 설정 파일 권한 제한(icacls) 실패: 종료코드 %s (%s)", proc.returncode, cfg_path)
        return f"파일 접근권한 제한(icacls) 실패: 종료코드 {proc.returncode}"
    return None


def save_mail_config(cfg: dict, path: str | Path | None = None) -> str | None:
    """화면(설정 탭)에서 입력한 메일 설정을 `mail.local.json` 에 저장한다.

    런타임 load/send 경로와 달리 **사용자가 직접 누른 저장 동작**이므로, 필수값이
    빠지면 조용히 넘어가지 않고 `ValueError`(한국어 메시지)를 던져 화면에 알린다.
    비밀번호는 어떤 로그/예외 메시지에도 넣지 않는다.

    저장 후 매번 파일 접근권한을 현재 사용자 전용으로 제한한다(`_restrict_acl`).
    반환값: 정상이면 `None`, 저장은 됐지만 권한 제한이 실패했으면 화면에 보여줄 경고 문구.
    """
    if not isinstance(cfg, dict):
        raise ValueError("메일 설정 형식이 올바르지 않습니다.")
    smtp = cfg.get("smtp")
    if not isinstance(smtp, dict):
        raise ValueError("SMTP 설정이 없습니다.")
    missing = [_FIELD_LABEL[k] for k in _REQUIRED_SMTP_KEYS if not smtp.get(k)]
    if not cfg.get("to_addr"):
        missing.append("수신 주소")
    if missing:
        raise ValueError("다음 항목을 입력하세요: " + ", ".join(missing))
    try:
        port = int(smtp["port"])
    except (TypeError, ValueError):
        raise ValueError("포트는 숫자여야 합니다.") from None
    if not 1 <= port <= 65535:
        raise ValueError("포트는 1~65535 범위여야 합니다.")

    cfg_path = Path(path) if path else MAIL_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("메일 설정 저장: %s (수신 %s)", cfg_path, cfg.get("to_addr"))
    return _restrict_acl(cfg_path)


def disable_mail_config(path: str | Path | None = None) -> bool:
    """메일 알림을 끈다 - `mail.local.json` 을 지운다(화면의 [메일 알림 끄기] 버튼).

    파일이 있어서 지웠으면 `True`, 원래 없었거나 지우지 못했으면 `False`.
    예외를 절대 던지지 않는다(삭제 실패는 경고 로그만 남긴다 - 호출 측은
    `load_mail_config()` 결과로 실제 꺼졌는지 다시 확인한다).
    """
    cfg_path = Path(path) if path else MAIL_CONFIG_PATH
    try:
        cfg_path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("메일 설정 파일 삭제 실패: %s (%s)", type(exc).__name__, cfg_path)
        return False
    log.info("메일 알림 끔: 설정 파일 삭제 %s", cfg_path)
    return True


def send_test_mail(cfg: dict) -> tuple[bool, str]:
    """SMTP 연동 테스트 메일을 **동기** 발송하고 (성공 여부, 메시지) 를 돌려준다.

    디스크가 아닌 넘겨받은 `cfg`(화면 입력값)를 그대로 쓰므로 저장 전에 검증할 수 있다.
    예외를 절대 밖으로 던지지 않는다. 호출 측(UI)은 별도 스레드에서 호출해야 한다.
    """
    if not _is_valid(cfg):
        return False, "필수 항목(SMTP 호스트/포트/아이디/비밀번호/발신 주소/수신 주소)을 모두 입력하세요."
    smtp = cfg["smtp"]
    try:
        msg = EmailMessage()
        msg["Subject"] = "[주식 자동매매] SMTP 연동 테스트 - 실제 체결 아님"
        from_name = smtp.get("from_name") or "주식 자동매매 알림"
        msg["From"] = f"{from_name} <{smtp['from_addr']}>"
        msg["To"] = cfg["to_addr"]
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg.set_content(
            "이 메일은 설정 화면의 '테스트 메일 발송' 버튼으로 보낸 SMTP 연동 테스트입니다.\n"
            "실제 체결(거래)이 발생한 것이 아닙니다.\n\n"
            f"발송시각: {now}\n"
            f"SMTP: {smtp['host']}:{smtp['port']}\n"
        )
        with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=10) as client:
            client.starttls()
            client.login(smtp["user"], smtp["password"])
            client.send_message(msg)
    except Exception as exc:  # noqa: BLE001 - 결과를 UI 로 돌려주고 예외는 삼킨다
        # 예외 문자열에 비밀번호가 섞일 가능성에 대비해 마스킹한다.
        text = f"{type(exc).__name__}: {exc}"
        pw = str(smtp.get("password") or "")
        if pw:
            text = text.replace(pw, "****")
        log.warning("테스트 메일 발송 실패: %s", text)
        return False, text
    log.info("테스트 메일 발송 성공 (수신 %s)", cfg.get("to_addr"))
    return True, "발송 성공"


def _build_message(cfg: dict, *, side: str, stk_cd: str, stk_nm: str | None,
                   qty: int, price: int, executed_at: _dt.datetime) -> EmailMessage:
    smtp = cfg["smtp"]
    side_label = _SIDE_LABEL.get(side, side)
    name = stk_nm or stk_cd
    qty = int(qty)
    price = int(price)
    total = qty * price
    if isinstance(executed_at, _dt.datetime):
        when = executed_at.strftime("%Y-%m-%d %H:%M:%S")
    else:
        when = str(executed_at)

    msg = EmailMessage()
    msg["Subject"] = (f"[주식 자동매매] 체결: {side_label} {name}({stk_cd}) "
                      f"{qty}주 @{price:,}원")
    from_name = smtp.get("from_name") or "주식 자동매매 알림"
    msg["From"] = f"{from_name} <{smtp['from_addr']}>"
    msg["To"] = cfg["to_addr"]
    body = (
        f"체결이 발생했습니다.\n\n"
        f"종목: {name}({stk_cd})\n"
        f"구분: {side_label}\n"
        f"체결시각: {when} (KST)\n"
        f"수량: {qty:,}주\n"
        f"단가: {price:,}원\n"
        f"총액: {total:,}원\n\n"
        f"※ 이 메일은 자동 발송된 참고용 알림입니다. 실제 체결 확인은 증권사 앱/HTS 를 기준으로 하세요.\n"
    )
    msg.set_content(body)
    return msg


def _send_sync(cfg: dict, *, side: str, stk_cd: str, stk_nm: str | None,
               qty: int, price: int, executed_at: _dt.datetime) -> None:
    """실제 SMTP 발송 (동기, 블로킹). 예외를 절대 밖으로 던지지 않는다.

    스레드로 감싸지 않은 순수 함수라 테스트/일회성 점검에서 직접 호출하기 쉽다.
    """
    smtp = cfg.get("smtp") or {}
    try:
        msg = _build_message(cfg, side=side, stk_cd=stk_cd, stk_nm=stk_nm, qty=qty,
                             price=price, executed_at=executed_at)
        with smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=10) as client:
            client.starttls()
            client.login(smtp["user"], smtp["password"])
            client.send_message(msg)
        log.info("체결 알림 메일 발송 완료: %s %s(%s) %s주 @%s", side, stk_nm or stk_cd,
                 stk_cd, qty, price)
    except Exception:  # noqa: BLE001 - 메일 실패가 주문/체결 처리에 영향을 주면 안 된다
        log.warning("체결 알림 메일 발송 실패 (%s %s)", side, stk_cd, exc_info=True)


def send_trade_completed_mail(cfg: dict | None, *, side: str, stk_cd: str,
                              stk_nm: str | None, qty: int, price: int,
                              executed_at: _dt.datetime) -> None:
    """체결 완료 알림 메일을 백그라운드 스레드로 발송한다.

    WS 메시지 처리 경로에서 호출되므로 **절대 그 자리에서 블로킹하지 않는다** - 실제
    네트워크 송신은 데몬 스레드에서 수행하고, 그 스레드 안에서 예외를 모두 삼킨다.
    `cfg` 가 없거나 필수값이 빠지면 아무 것도 하지 않고 즉시 반환한다(무조건 호출해도 안전).
    """
    if not _is_valid(cfg):
        log.debug("메일 설정 없음/불완전 - 체결 알림 메일 생략 (%s %s)", side, stk_cd)
        return
    threading.Thread(
        target=_send_sync,
        kwargs={"cfg": cfg, "side": side, "stk_cd": stk_cd, "stk_nm": stk_nm,
                "qty": qty, "price": price, "executed_at": executed_at},
        daemon=True,
    ).start()
