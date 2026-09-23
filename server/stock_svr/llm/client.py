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

# ---------------------------------------------------------------------- #
# 산업 트렌드 스캔(claude_trend_scan) 용 상수
# ---------------------------------------------------------------------- #
# 서버측 웹 검색 도구. 결과 블록은 `web_search_tool_result` 로 돌아오고,
# 검색 자체가 실패해도 HTTP 는 200 이며 블록 안에 오류 객체가 담긴다(치명적이지 않다).
WEB_SEARCH_TOOL = "web_search_20260209"
# 조사(1단계) 응답은 보고서 형식이라 길다
RESEARCH_MAX_OUTPUT_TOKENS = 8000
# 구조화 추출(2단계) 응답은 후보 JSON 하나
EXTRACT_MAX_OUTPUT_TOKENS = 4096
# 서버측 도구 루프가 10회를 넘으면 stop_reason='pause_turn' 으로 끊긴다 → 이어받기 상한
MAX_CONTINUATIONS = 3

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


@dataclass
class ClaudeResult:
    """트렌드 스캔용 공통 응답(1단계 조사 / 2단계 구조화 추출)."""

    text: str = ""
    data: Any = None                   # 2단계에서 파싱된 JSON
    model: str = ""
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    web_search_count: int = 0
    search_errors: list[str] = field(default_factory=list)
    # `web_search_tool_result` 블록 집계 (성공 = 결과 리스트, 실패 = 오류 객체)
    search_ok_count: int = 0
    search_fail_count: int = 0

    @property
    def web_search_attempted(self) -> bool:
        """웹 검색 도구를 실제로 돌렸는지(결과 블록이 하나라도 있었는지)."""
        return (self.search_ok_count + self.search_fail_count) > 0

    @property
    def web_search_all_failed(self) -> bool:
        """검색을 시도했는데 **성공한 검색이 한 건도 없는** 상태.

        이 경우 조사 본문은 모델의 사전 지식만으로 쓰인 것이므로 신뢰도가 낮다.
        호출자는 이번 스캔을 최소 'partial' 로 기록해야 한다.
        """
        return self.search_fail_count > 0 and self.search_ok_count == 0


def _tool_result_error(block) -> str:
    """`web_search_tool_result` 안의 오류 객체(HTTP 는 200)를 문자열로. 정상이면 빈 문자열."""
    content = getattr(block, "content", None)
    if content is None or isinstance(content, list):
        return ""
    if isinstance(content, dict):
        ctype, code = content.get("type"), content.get("error_code")
    else:
        ctype, code = getattr(content, "type", None), getattr(content, "error_code", None)
    if ctype and "error" in str(ctype):
        return str(code or ctype)[:60]
    return ""


def count_web_search_outcomes(msg) -> tuple[int, int]:
    """응답 1건의 `web_search_tool_result` 블록을 (성공수, 실패수) 로 센다.

    **서버측 도구의 실패는 예외로 오지 않는다** — HTTP 는 200 이고, 결과 블록의
    `content` 가 결과 리스트 대신 오류 객체(`{type: 'web_search_tool_result_error',
    error_code: ...}`) 로 온다. 그래서 `content` 의 모양으로만 성공/실패를 가른다.

    * 성공: `content` 가 검색 결과 **리스트**
    * 실패: `content` 가 `error_code` 를 가진 오류 객체 (예: `max_uses_exceeded`)

    호출 자체를 안 했으면 (0, 0) 이다.
    """
    ok = fail = 0
    for block in getattr(msg, "content", None) or []:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        if _tool_result_error(block):
            fail += 1
        else:
            ok += 1
    return ok, fail


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

    # ================================================================== #
    # 산업 트렌드 스캔 (claude_trend_scan) — 2단계 호출
    # ================================================================== #
    def research(self, prompt: str, *, model: str, system: str, effort: str = "medium",
                 timeout_sec: float = 90.0, max_web_searches: int = 6) -> ClaudeResult:
        """1단계: 서버측 웹 검색 도구를 켜고 자유 텍스트 조사 결과를 받는다.

        구조화 출력을 강제하지 않는다(조사 결과는 2단계에서 다시 걸러낸다).
        서버측 도구 루프가 멈추면(`stop_reason='pause_turn'`) 같은 대화를 그대로 다시 보내
        이어받는다(추가 user 메시지를 붙이지 않는다).
        """
        if model not in MODELS:
            raise ClaudeError(f"허용되지 않은 모델: {model}", infra=False)
        client = self._sdk()
        tools = [{"type": WEB_SEARCH_TOOL, "name": "web_search",
                  "max_uses": max(1, int(max_web_searches))}]
        kwargs: dict[str, Any] = {}
        if model in EFFORT_MODELS and effort in EFFORTS:
            kwargs["output_config"] = {"effort": effort}

        # 조사는 서버측 웹 검색을 여러 번 돌기 때문에 오래 걸린다. `timeout_sec` 를 나누면
        # 정상 응답도 잘려 나가므로 **요청 1건에 전체 예산을 준다**
        # (SDK 재시도가 있으면 최악의 경우 그 배수만큼 걸릴 수 있다).
        per_try = max(10.0, float(timeout_sec))
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        result = ClaudeResult(model=model)
        started = time.monotonic()
        for _ in range(MAX_CONTINUATIONS + 1):
            try:
                msg = client.messages.create(
                    model=model, max_tokens=RESEARCH_MAX_OUTPUT_TOKENS, system=system,
                    messages=messages, tools=tools, timeout=per_try, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 아래에서 SDK 예외 종류별로 분류
                raise self._classify(exc) from None
            self._absorb(result, msg)
            stop = getattr(msg, "stop_reason", None)
            if stop == "refusal":
                raise ClaudeError("모델이 조사를 거부함(refusal)", infra=False)
            if stop == "max_tokens":
                raise ClaudeError("조사 응답이 max_tokens 에서 잘림", infra=False)
            if stop != "pause_turn":
                break
            messages = [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": getattr(msg, "content", None) or []}]
        else:
            raise ClaudeError(f"조사가 이어받기 {MAX_CONTINUATIONS}회 후에도 끝나지 않음", infra=True)

        result.latency_ms = int((time.monotonic() - started) * 1000)
        result.text = result.text.strip()
        if not result.text:
            raise ClaudeError("조사 응답에 text 블록이 없음", infra=False)
        if result.web_search_all_failed:
            # 검색을 시도했는데 전부 실패 = 사실상 '검색 없이 쓴 보고서'
            log.error("웹 검색이 전부 실패했습니다(성공 0 / 실패 %d): %s"
                      " - 조사 결과는 모델의 사전 지식뿐이라 신뢰도가 낮습니다",
                      result.search_fail_count, ", ".join(result.search_errors[:5]))
        elif result.search_errors:
            log.warning("웹 검색 일부 실패: %s", ", ".join(result.search_errors[:5]))
        return result

    def extract(self, user_text: str, *, model: str, system: str, schema: dict,
                timeout_sec: float = 60.0,
                max_tokens: int = EXTRACT_MAX_OUTPUT_TOKENS) -> ClaudeResult:
        """2단계: 도구 없이 구조화 출력(json_schema)만 받아 JSON 으로 파싱한다."""
        if model not in MODELS:
            raise ClaudeError(f"허용되지 않은 모델: {model}", infra=False)
        client = self._sdk()
        attempts = self.max_retries + 1
        per_try = max(5.0, float(timeout_sec) / attempts)
        started = time.monotonic()
        try:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user_text}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
                timeout=per_try)
        except Exception as exc:  # noqa: BLE001
            raise self._classify(exc) from None

        result = ClaudeResult(model=model)
        self._absorb(result, msg)
        result.latency_ms = int((time.monotonic() - started) * 1000)
        stop = getattr(msg, "stop_reason", None)
        if stop == "refusal":
            raise ClaudeError("모델이 응답을 거부함(refusal)", infra=False)
        if stop == "max_tokens":
            raise ClaudeError("응답이 max_tokens 에서 잘림", infra=False)
        try:
            result.data = json.loads(self._first_text(msg))
        except (TypeError, ValueError) as exc:
            raise ClaudeError(f"응답 JSON 파싱 실패: {type(exc).__name__}", infra=False) from None
        return result

    # ------------------------------------------------------------------ #
    @staticmethod
    def _absorb(result: ClaudeResult, msg) -> None:
        """응답 1건의 text·토큰·웹검색 횟수를 결과에 누적한다."""
        texts: list[str] = []
        searches = 0
        for block in getattr(msg, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                texts.append(getattr(block, "text", "") or "")
            elif btype == "server_tool_use":
                if getattr(block, "name", "") == "web_search":
                    searches += 1
            elif btype == "web_search_tool_result":
                err = _tool_result_error(block)
                if err:
                    result.search_errors.append(err)
                    result.search_fail_count += 1
                else:
                    result.search_ok_count += 1
        body = "\n".join(t for t in texts if t)
        if body:
            result.text = f"{result.text}\n{body}" if result.text else body

        usage = getattr(msg, "usage", None)
        result.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        result.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        # usage.server_tool_use.web_search_requests 가 있으면 그 값이 정본이다
        reported = None
        stu = getattr(usage, "server_tool_use", None)
        if stu is not None:
            try:
                reported = int(getattr(stu, "web_search_requests", 0) or 0)
            except (TypeError, ValueError):
                reported = None
        result.web_search_count += reported if reported else searches
        result.model = str(getattr(msg, "model", "") or result.model)

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
