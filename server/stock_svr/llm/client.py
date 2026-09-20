"""Anthropic(Claude) Messages API 호출 래퍼.

* SDK(`anthropic`)는 **호출 시점에만** import 한다(미설치 환경에서도 서버가 기동되도록).
* API 키는 설정이 가리키는 파일에서 읽어 메모리에만 두고, 로그·DB·예외 메시지에 남기지 않는다.
* 구조화 출력(`output_config.format` = json_schema)을 쓰고 응답은 **JSON 으로 파싱**한다
  (문자열 매칭 금지). 파싱 결과는 `prompt.validate_output()` 으로 한 번 더 검증한다.
* 오류는 SDK 예외를 구체적 → 일반 순으로 잡아 **인프라 오류/그 외**로만 분류한다.
  인프라 오류(연결·타임아웃·429·5xx)만 `fail_mode=allow` 의 대상이고,
  거부(refusal)·max_tokens·무효 JSON·4xx 는 어떤 설정에서도 차단한다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..util import mask_text
from .prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, OutputSchemaError, validate_output

log = logging.getLogger(__name__)

# 허용 모델 (날짜 접미사 금지 - 별칭 그대로 사용)
MODEL_OPUS = "claude-opus-5"
MODEL_SONNET = "claude-sonnet-5"
MODEL_HAIKU = "claude-haiku-4-5"
MODELS = (MODEL_OPUS, MODEL_SONNET, MODEL_HAIKU)

# `output_config.effort` 를 지원하는 모델 (Models API 확인 결과 haiku-4-5 는 미지원 → 400)
EFFORT_MODELS = frozenset({MODEL_OPUS, MODEL_SONNET})
EFFORTS = ("low", "medium", "high")

# 판단 JSON 하나면 충분하다
MAX_OUTPUT_TOKENS = 1024

# Opus 5 는 temperature/top_p/top_k 와 thinking.budget_tokens 가 제거되어 전달 시 400 이다.
# → 샘플링 파라미터를 보내지 않고 thinking 도 생략한다(기본 adaptive).


class ClaudeError(RuntimeError):
    """Claude 호출/응답 오류.

    `infra=True` 는 연결·타임아웃·429·5xx 등 **인프라 오류**로, 이때만 `fail_mode=allow` 가
    적용된다. 거부/max_tokens/무효 응답/4xx 는 `infra=False` 로 항상 차단된다.
    """

    def __init__(self, message: str, infra: bool = False):
        super().__init__(mask_text(str(message))[:200])
        self.infra = bool(infra)


@dataclass
class ClaudeReview:
    """모델이 돌려준 검토 결과(정상 응답)."""

    decision: str                      # 'allow' | 'block'
    confidence: int                    # 0~100
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def reasons_text(self) -> str:
        return " / ".join(self.reasons) if self.reasons else "(근거 없음)"


def _default_factory(api_key: str, max_retries: int):
    import anthropic  # 지연 import (미설치 시 ClaudeError 로 변환)

    return anthropic.Anthropic(api_key=api_key, max_retries=max_retries)


class ClaudeClient:
    """Messages API 호출 1건 = 검토 1건."""

    def __init__(self, key_provider: Callable[[], str], max_retries: int = 1,
                 client_factory: Callable[[str, int], Any] | None = None):
        self._key_provider = key_provider
        self.max_retries = max(0, int(max_retries))
        self._factory = client_factory or _default_factory
        self._client: Any = None

    # ------------------------------------------------------------------ #
    def _sdk(self):
        if self._client is None:
            try:
                key = self._key_provider()
            except Exception as exc:  # noqa: BLE001 - 설정/파일 오류 (키 값은 남기지 않는다)
                raise ClaudeError(f"API 키를 읽을 수 없음: {type(exc).__name__}", infra=False) from None
            try:
                self._client = self._factory(key, self.max_retries)
            except ImportError:
                raise ClaudeError("anthropic SDK 가 설치되어 있지 않음", infra=False) from None
            except Exception as exc:  # noqa: BLE001
                raise ClaudeError(f"Claude 클라이언트 생성 실패: {type(exc).__name__}",
                                  infra=False) from None
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        try:
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def review(self, payload: dict, model: str, effort: str = "low",
               timeout_sec: float = 30.0) -> ClaudeReview:
        """입력 JSON 1건을 검토시키고 결과를 돌려준다. 실패는 `ClaudeError`."""
        if model not in MODELS:
            raise ClaudeError(f"허용되지 않은 모델: {model}", infra=False)
        client = self._sdk()

        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        }
        if model in EFFORT_MODELS and effort in EFFORTS:
            output_config["effort"] = effort

        # 재시도까지 합쳐도 timeout_sec 를 크게 넘지 않도록 요청 1건의 제한시간을 나눈다
        attempts = self.max_retries + 1
        per_try = max(3.0, float(timeout_sec) / attempts)

        started = time.monotonic()
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user",
                           "content": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
                output_config=output_config,
                timeout=per_try,
            )
        except Exception as exc:  # noqa: BLE001 - 아래에서 SDK 예외 종류별로 분류
            raise self._classify(exc) from None
        latency_ms = int((time.monotonic() - started) * 1000)

        stop = getattr(msg, "stop_reason", None)
        if stop == "refusal":
            raise ClaudeError("모델이 응답을 거부함(refusal)", infra=False)
        if stop == "max_tokens":
            raise ClaudeError("응답이 max_tokens 에서 잘림", infra=False)

        text = self._first_text(msg)
        try:
            data = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise ClaudeError(f"응답 JSON 파싱 실패: {type(exc).__name__}", infra=False) from None
        try:
            parsed = validate_output(data)
        except OutputSchemaError as exc:
            raise ClaudeError(f"응답 스키마 위반: {exc}", infra=False) from None

        usage = getattr(msg, "usage", None)
        return ClaudeReview(
            decision=parsed["decision"],
            confidence=parsed["confidence"],
            reasons=parsed["reasons"],
            risk_flags=parsed["risk_flags"],
            model=str(getattr(msg, "model", "") or model),
            latency_ms=latency_ms,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _first_text(msg) -> str:
        """구조화 출력이면 첫 text 블록이 곧 JSON 이다."""
        for block in getattr(msg, "content", None) or []:
            if getattr(block, "type", None) == "text":
                return getattr(block, "text", "") or ""
        raise ClaudeError("응답에 text 블록이 없음", infra=False)

    @staticmethod
    def _classify(exc: Exception) -> ClaudeError:
        """SDK 예외를 구체적 → 일반 순으로 분류한다(메시지에 키가 섞이지 않게 마스킹)."""
        if isinstance(exc, ClaudeError):
            return exc
        try:
            import anthropic
        except ImportError:
            return ClaudeError("anthropic SDK 가 설치되어 있지 않음", infra=False)

        name = type(exc).__name__
        if isinstance(exc, anthropic.APITimeoutError):          # 요청 시간 초과
            return ClaudeError("Claude 호출 시간 초과", infra=True)
        if isinstance(exc, anthropic.RateLimitError):           # 429
            return ClaudeError("Claude 호출량 제한(429)", infra=True)
        if isinstance(exc, anthropic.APIConnectionError):       # 네트워크
            return ClaudeError(f"Claude 연결 실패({name})", infra=True)
        if isinstance(exc, anthropic.APIStatusError):
            status = int(getattr(exc, "status_code", 0) or 0)
            if status >= 500:
                return ClaudeError(f"Claude 서버 오류({status})", infra=True)
            return ClaudeError(f"Claude 요청 오류({status} {name})", infra=False)
        if isinstance(exc, anthropic.APIError):
            return ClaudeError(f"Claude API 오류({name})", infra=False)
        return ClaudeError(f"Claude 호출 실패({name}: {exc})", infra=False)
