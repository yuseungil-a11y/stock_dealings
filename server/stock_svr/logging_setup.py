"""로깅 구성.

* 파일: `server/logs/stock_svr.log` (일 단위 회전, backupCount=retention_days)
* 시작 시 / 회전 시 보관기간 초과 파일 삭제
* 비밀값 마스킹 필터 (appkey/secretkey/token/password/Bearer)
* UI 로 이벤트를 흘려보내는 큐 핸들러
"""
from __future__ import annotations

import datetime as _dt
import logging
import logging.handlers
import queue
import re
from pathlib import Path

from .config import LoggingConfig
from .util import mask_text

LOG_NAME = "stock_svr"
LOG_FILE = "stock_svr.log"
FMT = "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s"

# UI 가 소비하는 이벤트 큐 (maxsize 로 폭주 방지)
ui_queue: "queue.Queue[dict]" = queue.Queue(maxsize=5000)


class MaskFilter(logging.Filter):
    """포맷된 메시지에서 비밀값을 지운다."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        masked = mask_text(msg)
        if masked != msg:
            record.msg = masked
            record.args = ()
        return True


class UiQueueHandler(logging.Handler):
    """로그를 UI 큐로 전달 (블로킹 금지 - 가득 차면 버림)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "ts": _dt.datetime.fromtimestamp(record.created),
                "level": record.levelname,
                "category": _category_of(record.name),
                "message": mask_text(record.getMessage()),
            }
            ui_queue.put_nowait(item)
        except queue.Full:
            pass
        except Exception:  # noqa: BLE001
            pass


class DbEventHandler(logging.Handler):
    """WARNING 이상 및 표시된 레코드를 event_log 에 기록."""

    def __init__(self, db, level=logging.WARNING):
        super().__init__(level)
        self.db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname
            if level == "WARNING":
                level = "WARN"
            elif level == "CRITICAL":
                level = "ERROR"
            if level not in ("DEBUG", "INFO", "WARN", "ERROR"):
                level = "INFO"
            self.db.log_event(level, _category_of(record.name), mask_text(record.getMessage()))
        except Exception:  # noqa: BLE001 - DB 로깅 실패가 프로그램을 막지 않게
            pass


def _category_of(logger_name: str) -> str:
    """로거 이름 -> event_log.category."""
    name = logger_name.split(".")
    if "kiwoom" in name:
        if "ws" in name:
            return "ws"
        if "auth" in name:
            return "auth"
        return "kiwoom"
    for part in ("services", "engine", "algo", "ui"):
        if part in name:
            return {"services": "sync", "engine": "engine", "algo": "algo", "ui": "ui"}[part]
    return "system"


_ROTATED_RE = re.compile(r"^stock_svr\.log\.(\d{4}-\d{2}-\d{2})$")


def purge_old_logs(log_dir: Path, retention_days: int) -> int:
    """보관기간(일)을 넘긴 회전 로그 파일 삭제."""
    if not log_dir.exists():
        return 0
    cutoff = _dt.date.today() - _dt.timedelta(days=max(1, retention_days))
    removed = 0
    for p in log_dir.glob("stock_svr.log.*"):
        m = _ROTATED_RE.match(p.name)
        stamp = None
        if m:
            try:
                stamp = _dt.date.fromisoformat(m.group(1))
            except ValueError:
                stamp = None
        if stamp is None:
            stamp = _dt.date.fromtimestamp(p.stat().st_mtime)
        if stamp < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def setup_logging(cfg: LoggingConfig, console: bool = True, to_ui: bool = True) -> logging.Logger:
    log_dir = cfg.path
    log_dir.mkdir(parents=True, exist_ok=True)
    purge_old_logs(log_dir, cfg.retention_days)

    root = logging.getLogger(LOG_NAME)
    root.setLevel(getattr(logging, cfg.level, logging.INFO))
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass

    fmt = logging.Formatter(FMT)
    mask = MaskFilter()

    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / LOG_FILE, when="midnight", backupCount=max(1, cfg.retention_days),
        encoding="utf-8", delay=False)
    fh.suffix = "%Y-%m-%d"
    fh.setFormatter(fmt)
    fh.addFilter(mask)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.addFilter(mask)
        root.addHandler(ch)

    if to_ui:
        uh = UiQueueHandler()
        uh.addFilter(mask)
        root.addHandler(uh)

    return root


def attach_db_handler(db, level=logging.WARNING) -> DbEventHandler:
    root = logging.getLogger(LOG_NAME)
    for h in list(root.handlers):
        if isinstance(h, DbEventHandler):
            root.removeHandler(h)
    handler = DbEventHandler(db, level)
    handler.addFilter(MaskFilter())
    root.addHandler(handler)
    return handler


def get_logger(name: str) -> logging.Logger:
    """`stock_svr.<name>` 하위 로거."""
    if name.startswith(LOG_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOG_NAME}.{name}")
