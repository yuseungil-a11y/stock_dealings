"""DART OpenAPI(opendart.fss.or.kr) 호출기 — **읽기 전용 GET 전용**.

키움 REST 클라이언트(`kiwoom/rest.py`)와 **별도 모듈**이다. 주문 API 개념 자체가 없고,
이 클라이언트로는 공시 재무데이터 조회 GET 만 나간다.

엔드포인트는 개발가이드(https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003)와
실제 1회 시험 호출로 확인한 것만 쓴다.

* `GET /api/corpCode.xml?crtfc_key=…`
  → ZIP(내부 `CORPCODE.xml`). `<list>` 반복이며 비상장사는 `stock_code` 가 공백이다.
* `GET /api/fnlttSinglAcntAll.json?crtfc_key=…&corp_code=…&bsns_year=…&reprt_code=…&fs_div=…`
  → 단일회사 **전체** 재무제표. `list[]` 의 `sj_div` 가 BS/IS/CIS/CF/SCE,
    `account_id` 는 IFRS 택사노미 ID(`ifrs-full_Revenue` 등)이라 계정명 문자열 매칭보다 안전하다.
    "단일회사 주요계정"(`fnlttSinglAcnt.json`)도 있지만 **EPS·영업활동현금흐름이 없어서**
    (실제 응답으로 확인) 전체 재무제표 쪽을 쓴다.

응답의 `status` 가 `"000"` 이 아니면 오류다. 단 `"013"`(조회된 데이타가 없습니다)은
**미공시 분기에서 정상적으로 나오는 값**이므로 `DartNoData` 로 따로 구분해 호출부가
에러로 취급하지 않게 한다.

보안: 인증키는 쿼리 파라미터로만 붙이고, **로그·예외 메시지에는 URL 쿼리를 절대 싣지 않는다**
(키움 앱키·Anthropic 키와 동일한 취급).
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from typing import Any, Callable

import httpx

from ..config import DartConfig
from ..kiwoom.ratelimit import RateLimiter
from ..util import mask_text

log = logging.getLogger(__name__)

PATH_CORP_CODE = "/api/corpCode.xml"
PATH_SINGLE_ACNT_ALL = "/api/fnlttSinglAcntAll.json"

# 개발가이드의 status 코드 (원문 그대로)
STATUS_OK = "000"
STATUS_NO_DATA = "013"
STATUS_TEXT = {
    "000": "정상",
    "010": "등록되지 않은 키입니다",
    "011": "사용할 수 없는 키입니다",
    "012": "접근할 수 없는 IP입니다",
    "013": "조회된 데이타가 없습니다",
    "014": "파일이 존재하지 않습니다",
    "020": "요청 제한을 초과하였습니다",
    "021": "조회 가능한 회사 개수가 초과하였습니다",
    "100": "필드의 부적절한 값입니다",
    "101": "부적절한 접근입니다",
    "800": "시스템 점검 중입니다",
    "900": "정의되지 않은 오류가 발생하였습니다",
    "901": "사용자 계정의 개인정보보유기간이 만료되었습니다",
}
# 잠시 후 다시 시도하면 되는 상태(인프라성)
RETRYABLE_STATUS = frozenset({"020", "800", "900"})

# 연결/5xx/타임아웃 재시도 횟수
MAX_RETRY = 2

# 보고서 코드 (개발가이드 reprt_code)
REPRT_Q1 = "11013"
REPRT_H1 = "11012"
REPRT_Q3 = "11014"
REPRT_ANNUAL = "11011"
REPRT_CODES = (REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_ANNUAL)

# 개별/연결 재무제표 구분. 연결이 없는 회사가 많아 CFS → OFS 순으로 시도한다.
FS_CONSOLIDATED = "CFS"
FS_SEPARATE = "OFS"
FS_DIVS = (FS_CONSOLIDATED, FS_SEPARATE)

CORP_CODE_XML_NAME = "CORPCODE.xml"


class DartError(RuntimeError):
    """DART 호출/응답 오류. 메시지에 인증키가 섞이지 않게 항상 마스킹한다.

    `infra=True` 는 연결·타임아웃·5xx·요청제한처럼 **잠시 후 다시 하면 되는** 오류다.
    """

    def __init__(self, message: str, *, status: str = "", infra: bool = False):
        super().__init__(mask_text(str(message))[:200])
        self.status = str(status or "")
        self.infra = bool(infra)


class DartNoData(DartError):
    """status='013' (조회된 데이타가 없습니다).

    아직 공시되지 않은 분기를 물었을 때 정상적으로 돌아오는 응답이므로
    호출부는 이것을 **오류로 취급하지 않는다**.
    """

    def __init__(self, message: str = "조회된 데이타가 없습니다"):
        super().__init__(message, status=STATUS_NO_DATA, infra=False)


def _default_factory(timeout_sec: float):
    return httpx.Client(timeout=timeout_sec)


class DartClient:
    """DART OpenAPI 호출기 (스레드 안전). GET 만 한다."""

    def __init__(self, cfg: DartConfig,
                 client_factory: Callable[[float], Any] | None = None):
        self.cfg = cfg
        self.limiter = RateLimiter(cfg.min_interval_sec)
        self._factory = client_factory or _default_factory
        self._client: Any = None
        self.calls = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ #
    def _http(self):
        if self._client is None:
            self._client = self._factory(self.cfg.http_timeout_sec)
        return self._client

    def close(self) -> None:
        client, self._client = self._client, None
        try:
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    def _get(self, path: str, params: dict[str, str]) -> httpx.Response:
        """GET 1건. 인증키는 여기서만 붙이고 예외 메시지에는 넣지 않는다."""
        try:
            key = self.cfg.read_key()
        except Exception as exc:  # noqa: BLE001 - 키 값은 남기지 않는다
            raise DartError(f"DART 인증키를 읽을 수 없음: {type(exc).__name__}") from None
        url = f"{self.cfg.base_url.rstrip('/')}{path}"
        query = dict(params)
        query["crtfc_key"] = key

        attempt = 0
        while True:
            attempt += 1
            self.limiter.acquire()
            started = time.perf_counter()
            try:
                resp = self._http().get(url, params=query)
            except httpx.HTTPError as exc:
                if attempt <= MAX_RETRY:
                    time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
                    continue
                self.last_error = type(exc).__name__
                # URL 에 인증키가 들어 있으므로 예외 객체(exc)를 메시지에 넣지 않는다
                raise DartError(f"DART 연결 실패({type(exc).__name__})", infra=True) from None
            self.calls += 1
            elapsed = int((time.perf_counter() - started) * 1000)
            status = resp.status_code
            if status != 200:
                if status in (429, 500, 502, 503, 504) and attempt <= MAX_RETRY:
                    self.limiter.penalize()
                    continue
                self.last_error = f"HTTP {status}"
                raise DartError(f"DART HTTP {status} ({path})", infra=status >= 500)
            log.debug("DART %s %dms", path, elapsed)
            self.limiter.relax()
            self.last_error = None
            return resp

    # ------------------------------------------------------------------ #
    def _json(self, path: str, params: dict[str, str]) -> dict:
        """JSON 응답을 받아 `status` 를 검사한다. '013' 은 `DartNoData`."""
        resp = self._get(path, params)
        try:
            data = resp.json()
        except ValueError:
            raise DartError(f"DART 응답이 JSON 이 아님 ({path})") from None
        if not isinstance(data, dict):
            raise DartError(f"DART 응답이 객체가 아님 ({path})")
        status = str(data.get("status") or "")
        if status == STATUS_OK:
            return data
        if status == STATUS_NO_DATA:
            raise DartNoData()
        text = STATUS_TEXT.get(status, "알 수 없는 오류")
        infra = status in RETRYABLE_STATUS
        if infra:
            self.limiter.penalize()
        # 응답 message 는 서버가 준 문자열이므로 마스킹 후 짧게만 싣는다(DartError 가 처리)
        raise DartError(f"DART 오류 status={status} ({text})", status=status, infra=infra)

    # ================================================================== #
    # 공개 API
    # ================================================================== #
    def corp_code_xml(self) -> str:
        """`corpCode.xml` (ZIP) 을 받아 내부 XML 텍스트를 돌려준다.

        응답 content-type 이 `application/x-msdownload` 라 확장자만으로는 판단할 수 없고,
        **항상 ZIP 바이너리**다. 오류일 때만 XML(`<result><status>…`) 이 그대로 온다.
        """
        resp = self._get(PATH_CORP_CODE, {})
        body = resp.content
        if not body:
            raise DartError("corpCode 응답이 비어 있음")
        try:
            zf = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile:
            # 오류 응답은 ZIP 이 아니라 XML 로 온다 → status 를 꺼내 같은 규칙으로 처리
            status = _xml_status(body)
            if status == STATUS_NO_DATA:
                raise DartNoData() from None
            text = STATUS_TEXT.get(status, "알 수 없는 오류")
            raise DartError(f"corpCode 오류 status={status} ({text})", status=status,
                            infra=status in RETRYABLE_STATUS) from None
        names = zf.namelist()
        name = CORP_CODE_XML_NAME if CORP_CODE_XML_NAME in names else (names[0] if names else "")
        if not name:
            raise DartError("corpCode ZIP 안에 파일이 없음")
        return zf.read(name).decode("utf-8", errors="replace")

    def single_account_all(self, corp_code: str, bsns_year: int | str, reprt_code: str,
                           fs_div: str = FS_CONSOLIDATED) -> list[dict]:
        """단일회사 **전체** 재무제표 1건(`fnlttSinglAcntAll.json`).

        미공시(status='013')면 `DartNoData` 를 올린다(정상 상황).
        """
        if str(reprt_code) not in REPRT_CODES:
            raise DartError(f"허용되지 않은 reprt_code: {reprt_code}")
        if str(fs_div) not in FS_DIVS:
            raise DartError(f"허용되지 않은 fs_div: {fs_div}")
        data = self._json(PATH_SINGLE_ACNT_ALL, {
            "corp_code": str(corp_code),
            "bsns_year": str(bsns_year),
            "reprt_code": str(reprt_code),
            "fs_div": str(fs_div),
        })
        rows = data.get("list")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _xml_status(body: bytes) -> str:
    """오류 XML(`<result><status>013</status>…`)에서 status 만 꺼낸다."""
    from xml.etree import ElementTree as ET

    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return ""
    return (root.findtext("status") or "").strip()
