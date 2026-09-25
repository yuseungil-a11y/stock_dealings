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
import smtplib
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
