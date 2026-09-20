"""알고리즘 레지스트리 (플러그인 구조).

`algorithm.code` 로 클래스를 찾아 DB 파라미터와 함께 인스턴스를 만든다.
"""
from __future__ import annotations

import logging
from typing import Callable

from .base import Algorithm
from .params import ParamSet

log = logging.getLogger(__name__)

# 파라미터 오류가 **조용히 넘어가면 안 되는** 알고리즘 (R-01).
# is_locked(항상 켜짐) 이거나 role='risk' 인 알고리즘이 빠지면 한도 검사가 통째로 사라진다.
CRITICAL_ROLES = ("risk",)

# on_error(code, detail, critical) — 레지스트리가 알고리즘을 비활성화할 때 호출된다.
BuildErrorHook = Callable[[str, str, bool], None]

_REGISTRY: dict[str, type[Algorithm]] = {}


def register(cls: type[Algorithm]) -> type[Algorithm]:
    if not cls.code:
        raise ValueError(f"{cls.__name__}.code 가 비어 있습니다.")
    _REGISTRY[cls.code] = cls
    return cls


def get(code: str) -> type[Algorithm] | None:
    _ensure_loaded()
    return _REGISTRY.get(code)


def known_codes() -> list[str]:
    _ensure_loaded()
    return sorted(_REGISTRY)


def is_critical(meta: dict) -> bool:
    """이 알고리즘이 빠지면 안전장치가 사라지는가 (R-01)."""
    return bool(meta.get("is_locked")) or (meta.get("role") in CRITICAL_ROLES)


def build(meta: dict, on_error: BuildErrorHook | None = None) -> Algorithm | None:
    """DB 의 algorithm 행(+param_defs/params)으로 인스턴스 생성.

    실패(구현 없음/파라미터 오류)하면 None 을 돌려주고, `on_error` 가 있으면
    사유를 알려 호출부가 경보(ERROR 이벤트·UI 상태)를 낼 수 있게 한다 (R-01).
    """
    _ensure_loaded()
    code = str(meta.get("code") or "")
    cls = _REGISTRY.get(code)
    if cls is None:
        log.warning("구현이 없는 알고리즘 코드: %s", code)
        _notify(on_error, code, "구현(클래스)이 없습니다", is_critical(meta))
        return None
    params = ParamSet(meta.get("param_defs") or [], meta.get("params") or {})
    if params.invalid:
        # S-16: 저장된 파라미터가 정의(타입·min/max)를 벗어나면 알고리즘을 비활성화한다.
        detail = "; ".join(params.invalid[:5])
        log.error("알고리즘 %s 파라미터 오류로 비활성화: %s", code, detail)
        _notify(on_error, code, f"파라미터 오류: {detail}", is_critical(meta))
        return None
    return cls(meta=meta, params=params)


def _notify(hook: BuildErrorHook | None, code: str, detail: str, critical: bool) -> None:
    if hook is None:
        return
    try:
        hook(code, detail, critical)
    except Exception:  # noqa: BLE001 - 통지 실패가 빌드 경로를 죽이지 않게
        log.debug("알고리즘 빌드 오류 통지 실패", exc_info=True)


def build_all(metas: list[dict], enabled_only: bool = True,
              on_error: BuildErrorHook | None = None) -> list[Algorithm]:
    """우선순위(priority) 오름차순으로 인스턴스 목록 생성. is_locked 는 항상 포함."""
    out: list[Algorithm] = []
    for meta in metas:
        enabled = bool(meta.get("is_enabled")) or bool(meta.get("is_locked"))
        if enabled_only and not enabled:
            continue
        algo = build(meta, on_error=on_error)
        if algo is not None:
            out.append(algo)
    out.sort(key=lambda a: (a.priority, a.code))
    return out


_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import (  # noqa: F401  (import 시점에 @register 실행)
        averaging_down,
        claude_advisor,
        ma_cross_filter,
        momentum_screen,
        risk_guard,
        volatility_breakout,
    )
