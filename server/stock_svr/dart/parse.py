"""DART 응답 파서 (순수 함수 — 네트워크·DB 접근 없음).

`corpCode.xml` 의 `<list>` 반복과 `fnlttSinglAcntAll.json` 의 `list[]` 를
DB 컬럼(`company_corp_code` / `company_financial`)에 바로 넣을 수 있는 모양으로 바꾼다.

계정 식별은 **`account_id`(IFRS 택사노미 ID) 우선**이다. 계정명(`account_nm`)은 회사마다
표기가 달라(`매출액` / `수익(매출액)` / `영업수익` …) 문자열 매칭이 깨지기 쉽기 때문이며,
`account_id` 가 비표준(`-dart_…` 등)인 경우에만 계정명 후보로 보조 판정한다.

금액 해석 (실제 응답으로 확인)
* `BS`(재무상태표): `thstrm_amount` = 기말 잔액.
* `IS`/`CIS`(손익): 분기·반기 보고서의 `thstrm_amount` 는 **당기 3개월** 금액이고
  `thstrm_add_amount` 가 누적이다. 사업보고서(11011)는 `thstrm_amount` 가 연간 금액이다.
  3개월 칸을 비워 두는 회사가 있어 값이 없으면 누적값으로 대체한다(그때는 누적이 들어간다).
* `CF`(현금흐름표): `thstrm_amount` 는 그 보고서 기간의 **누적** 현금흐름이다.
"""
from __future__ import annotations

import io
import logging
from typing import Iterator
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------- #
# 계정 매핑 (account_id 우선, 계정명은 보조)
# ---------------------------------------------------------------------- #
SJ_BS = "BS"
SJ_IS = "IS"
SJ_CIS = "CIS"
SJ_CF = "CF"

# 필드 -> (허용 sj_div, account_id 후보, 계정명 후보)
ACCOUNT_MAP: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "total_assets": ((SJ_BS,), ("ifrs-full_Assets",), ("자산총계",)),
    "total_liabilities": ((SJ_BS,), ("ifrs-full_Liabilities",), ("부채총계",)),
    "total_equity": ((SJ_BS,), ("ifrs-full_Equity",), ("자본총계",)),
    "revenue": ((SJ_IS, SJ_CIS),
                ("ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"),
                ("매출액", "수익(매출액)", "영업수익")),
    "operating_profit": ((SJ_IS, SJ_CIS),
                         ("dart_OperatingIncomeLoss",
                          "ifrs-full_ProfitLossFromOperatingActivities"),
                         ("영업이익", "영업이익(손실)")),
    "net_profit": ((SJ_IS, SJ_CIS), ("ifrs-full_ProfitLoss",),
                   ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익")),
    "eps": ((SJ_IS, SJ_CIS), ("ifrs-full_BasicEarningsLossPerShare",),
            ("기본주당이익", "기본주당이익(손실)", "주당이익", "기본주당순이익")),
    "operating_cash_flow": ((SJ_CF,),
                            ("ifrs-full_CashFlowsFromUsedInOperatingActivities",),
                            ("영업활동현금흐름", "영업활동으로인한현금흐름")),
}
FINANCIAL_FIELDS = tuple(ACCOUNT_MAP)

# 재무상태표 계정은 잔액이므로 누적/당기 구분이 없다
_BALANCE_FIELDS = frozenset({"total_assets", "total_liabilities", "total_equity"})


def to_amount(value) -> int | None:
    """'333,605,938,000,000' / '-13,478,040,000,000' / '-' → int. 해석 불가면 None."""
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text or text in ("-", "--"):
        return None
    neg = False
    if text.startswith("(") and text.endswith(")"):   # 회계식 음수 표기
        neg, text = True, text[1:-1]
    try:
        out = int(float(text))
    except (TypeError, ValueError):
        return None
    return -out if neg else out


def _norm(text) -> str:
    return "".join(str(text or "").split())


def _matches(row: dict, ids: tuple[str, ...], names: tuple[str, ...]) -> bool:
    account_id = _norm(row.get("account_id"))
    if account_id and account_id in ids:
        return True
    if account_id and account_id not in ("-", ""):
        # 표준 ID 가 붙어 있는데 우리가 찾는 것이 아니면 계정명은 보지 않는다(오탐 방지)
        if account_id.startswith("ifrs") or account_id.startswith("dart_"):
            return False
    return _norm(row.get("account_nm")) in tuple(_norm(n) for n in names)


def _period_amount(row: dict, field: str) -> int | None:
    """그 보고서 기간에 해당하는 금액. 3개월 칸이 비면 누적으로 대체한다."""
    amount = to_amount(row.get("thstrm_amount"))
    if amount is not None or field in _BALANCE_FIELDS:
        return amount
    return to_amount(row.get("thstrm_add_amount"))


def parse_financial_rows(rows: list[dict]) -> dict[str, int | None]:
    """`fnlttSinglAcntAll.json` 의 `list[]` → `company_financial` 컬럼 값.

    같은 계정이 여러 번 나오면(예: IS 와 CIS 양쪽의 당기순이익) **먼저 나온 것**을 쓴다.
    한 건도 못 찾은 필드는 None(= DB NULL)이다.
    """
    out: dict[str, int | None] = {f: None for f in FINANCIAL_FIELDS}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        sj = _norm(row.get("sj_div")).upper()
        detail = _norm(row.get("account_detail"))
        if detail and detail not in ("-", "--"):
            continue          # 자본변동표식 세부 분해 행은 쓰지 않는다
        for field, (sjs, ids, names) in ACCOUNT_MAP.items():
            if out[field] is not None or sj not in sjs:
                continue
            if _matches(row, ids, names):
                value = _period_amount(row, field)
                if value is not None:
                    out[field] = value
    return out


def has_any_value(values: dict[str, int | None]) -> bool:
    """의미 있는 값이 하나라도 있는지(전부 None 이면 저장하지 않는다)."""
    return any(values.get(f) is not None for f in FINANCIAL_FIELDS)


# ---------------------------------------------------------------------- #
# corpCode.xml
# ---------------------------------------------------------------------- #
def iter_corp_codes(xml_text: str) -> Iterator[dict]:
    """`corpCode.xml` 의 `<list>` 를 하나씩 돌려준다(상장사만).

    전체 12만 건 / 약 28MB 라 `iterparse` 로 읽고 처리한 요소는 바로 버린다.
    `stock_code` 가 비어 있으면 비상장사이므로 건너뛴다.
    """
    source = io.BytesIO(xml_text.encode("utf-8"))
    for _, elem in ET.iterparse(source, events=("end",)):
        if elem.tag != "list":
            continue
        stock_code = (elem.findtext("stock_code") or "").strip()
        corp_code = (elem.findtext("corp_code") or "").strip()
        if stock_code and corp_code:
            yield {
                "stk_cd": stock_code,
                "corp_code": corp_code[:8],
                "corp_name": (elem.findtext("corp_name") or "").strip()[:120],
                "modify_date": (elem.findtext("modify_date") or "").strip(),
            }
        elem.clear()


def corp_codes_for(xml_text: str, wanted: set[str]) -> list[dict]:
    """대상 종목코드에 해당하는 매핑만 뽑는다(전체 상장사를 저장하지 않는다).

    같은 종목코드가 여러 번 나오면 `modify_date` 가 가장 최근인 것을 쓴다.
    """
    codes = {str(c).strip() for c in wanted if str(c or "").strip()}
    if not codes:
        return []
    best: dict[str, dict] = {}
    for row in iter_corp_codes(xml_text):
        code = row["stk_cd"]
        if code not in codes:
            continue
        prev = best.get(code)
        if prev is None or row["modify_date"] >= prev["modify_date"]:
            best[code] = row
    return [best[c] for c in sorted(best)]
