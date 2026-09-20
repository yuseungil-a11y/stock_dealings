"""다중 인스턴스 방지 (S-11 / R-08).

같은 계좌에 대해 두 프로세스가 동시에 주문을 내면 한도 계산이 어긋나므로 단일 실행을 강제한다.

* 락 파일은 **고정 경로**(`%LOCALAPPDATA%\\stock_svr\\stock_svr.lock`)에 만든다.
  exe 를 다른 폴더에 복사해 실행해도 같은 락을 잡는다.
* 생성은 `os.open(O_CREAT|O_EXCL)`, 보유는 OS 파일 잠금(msvcrt/fcntl)이며
  **핸들을 프로세스 수명 동안 열어 둔다**(PID 파일만 쓰던 기존 방식의 경쟁 구간 제거).
* 죽은 프로세스가 남긴 락은 자동으로 회수한다.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

LOCK_NAME = "stock_svr.lock"
APP_DIR_NAME = "stock_svr"

# 잠금은 PID 텍스트와 겹치지 않는 오프셋의 1바이트에 건다.
# (Windows 의 바이트 범위 잠금은 읽기도 막으므로, 겹치면 상대 PID 를 못 읽는다)
LOCK_OFFSET = 1024

# 프로세스 수명 동안 유지하는 락 핸들 {경로: fd}
_HELD: dict[str, int] = {}


class AlreadyRunningError(RuntimeError):
    """다른 인스턴스가 이미 실행 중."""


def lock_dir() -> Path:
    """락 폴더(고정 경로). exe 위치와 무관하다."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        base = tempfile.gettempdir()
    return Path(base) / APP_DIR_NAME


def lock_path() -> Path:
    return lock_dir() / LOCK_NAME


# 구버전(≤ R-08 이전)은 exe 옆 `logs\stock_svr.pid` 를 썼다. 새 빌드로 교체하는 시점에
# 구버전이 아직 떠 있으면 서로 다른 락을 잡아 **두 인스턴스가 동시에** 돌 수 있으므로,
# 기동 시 구버전 락도 함께 확인한다(읽기만 한다).
LEGACY_LOCK_NAME = "stock_svr.pid"


def legacy_lock_path() -> Path:
    from .config import SERVER_DIR

    return SERVER_DIR / "logs" / LEGACY_LOCK_NAME


def _legacy_running() -> int:
    try:
        p = legacy_lock_path()
        if not p.exists():
            return 0
        old = _read_pid(p)
    except Exception:  # noqa: BLE001 - 구버전 확인 실패가 기동을 막지 않게
        return 0
    if old and old != os.getpid() and _pid_alive(old):
        return old
    return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == 259      # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _try_lock(fd: int) -> bool:
    """LOCK_OFFSET 위치 1바이트에 비차단 배타 잠금. 실패하면 False."""
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _read_pid(p: Path) -> int:
    try:
        return int((p.read_text(encoding="utf-8") or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def acquire(path: Path | None = None) -> Path:
    """락 획득. 이미 살아 있는 인스턴스가 있으면 AlreadyRunningError."""
    p = path or lock_path()
    key = str(p)
    if key in _HELD:                      # 같은 프로세스에서 이미 보유 중
        return p
    if path is None:                      # 기본 경로로 기동할 때만 구버전 락도 본다
        legacy = _legacy_running()
        if legacy:
            raise AlreadyRunningError(
                f"PID {legacy} (구버전 락파일: {legacy_lock_path()}) - 이전 버전을 먼저 종료하세요")
    p.parent.mkdir(parents=True, exist_ok=True)

    try:
        fd = os.open(key, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except FileExistsError:
        # 기존 락 파일이 있다 → 주인이 살아 있는지 먼저 본다(사람이 읽을 수 있는 오류 메시지용).
        old = _read_pid(p)
        if old and old != os.getpid() and _pid_alive(old):
            raise AlreadyRunningError(f"PID {old} (락파일: {p})") from None
        try:
            fd = os.open(key, os.O_RDWR)          # 죽은 PID → 회수 시도
        except OSError as exc:
            raise AlreadyRunningError(f"락 파일을 열 수 없습니다: {p} ({exc})") from None

    # PID 판정과 무관하게 **OS 잠금**이 최종 판정이다(동시 기동 경쟁 차단).
    if not _try_lock(fd):
        old = _read_pid(p)
        os.close(fd)
        raise AlreadyRunningError(f"PID {old or '?'} 가 락을 보유 중 (락파일: {p})")

    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(os.getpid()).encode("ascii"))
    except OSError:
        log.debug("락 파일 PID 기록 실패", exc_info=True)
    _HELD[key] = fd                        # 프로세스가 끝날 때까지 핸들 유지
    return p


def release(path: Path | None = None) -> None:
    p = path or lock_path()
    key = str(p)
    fd = _HELD.pop(key, None)
    if fd is not None:
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        if p.exists() and _read_pid(p) == os.getpid():
            p.unlink()
    except OSError:
        pass


@contextlib.contextmanager
def single_instance(path: Path | None = None) -> Iterator[Path]:
    p = acquire(path)
    try:
        yield p
    finally:
        release(p)
