"""밸류에이션 계산 (순수 함수 — 네트워크·DB 접근 없음).

`company_financial` 행 묶음 + 현재 주가 + 발행주식수 → PER/PBR/ROE/부채비율.

**모든 수치는 여기(Python)에서 계산한다.** Claude 에게는 계산 결과만 주고 해석만 시킨다.

분모가 0·음수이거나 원천 데이터가 없으면 그 지표만 `None`(= DB NULL)이다. 오류가 아니다.

보고서 기간 규칙
* `11013`(1분기)=Q1, `11012`(반기)=Q2, `11014`(3분기)=Q3, `11011`(사업보고서)=연간.
* 손익 계정의 저장값은 **그 보고서 기간의 금액**이다(분기 보고서 = 당기 3개월,
  사업보고서 = 연간). 그래서 4분기 단독 금액은 `연간 - (Q1+Q2+Q3)` 로 만든다.
* TTM(최근 4개 분기 합산)은 연속된 4개 분기가 있을 때만 계산하고, 없으면
  **가장 최근 사업보고서의 연간 값**을 그대로 쓴다(그것도 없으면 None).
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from .client import REPRT_ANNUAL, REPRT_H1, REPRT_Q1, REPRT_Q3

log = logging.getLogger(__name__)

# 보고서 코드 → 분기 번호 (사업보고서는 연간이라 4분기 자리를 차지한다)
QUARTER_OF = {REPRT_Q1: 1, REPRT_H1: 2, REPRT_Q3: 3, REPRT_ANNUAL: 4}
REPRT_LABEL = {REPRT_Q1: "1분기", REPRT_H1: "반기", REPRT_Q3: "3분기", REPRT_ANNUAL: "사업보고서"}

# TTM 계산에 필요한 분기 수
TTM_QUARTERS = 4


def period_key(row) -> tuple[int, int]:
    """정렬용 (사업연도, 분기). 알 수 없으면 (0, 0)."""
    try:
        year = int(row.get("bsns_year") or 0)
    except (TypeError, ValueError):
        year = 0
    return year, QUARTER_OF.get(str(row.get("reprt_code") or ""), 0)


def period_label(row) -> str:
    """`financial_asof` 에 넣을 표기. 예: '2026Q2', '2025FY'."""
    year, quarter = period_key(row)
    if not year:
        return ""
    if str(row.get("reprt_code") or "") == REPRT_ANNUAL:
        return f"{year}FY"
    return f"{year}Q{quarter}" if quarter else str(year)


def sort_rows(rows) -> list[dict]:
    """과거 → 최신 순으로 정렬한 사본."""
    return sorted((dict(r) for r in rows or []), key=period_key)


def latest_row(rows) -> dict | None:
    """가장 최근 보고서 행(재무상태표 값의 기준)."""
    ordered = sort_rows(rows)
    return ordered[-1] if ordered else None


def annual_rows(rows) -> list[dict]:
    """사업보고서(연간) 행만 과거 → 최신 순으로."""
    return [r for r in sort_rows(rows) if str(r.get("reprt_code") or "") == REPRT_ANNUAL]


def _num(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return None


def quarter_values(rows, field: str) -> dict[tuple[int, int], int]:
    """{(연도, 분기): 그 분기 단독 금액}.

    4분기는 `연간 - (Q1+Q2+Q3)` 로 만든다(세 분기가 모두 있을 때만).
    """
    by_period: dict[tuple[int, int], int] = {}
    annual: dict[int, int] = {}
    for row in rows or []:
        value = _num(row.get(field))
        if value is None:
            continue
        year, quarter = period_key(row)
        if not year or not quarter:
            continue
        if str(row.get("reprt_code") or "") == REPRT_ANNUAL:
            annual[year] = value
        else:
            by_period[(year, quarter)] = value
    for year, total in annual.items():
        parts = [by_period.get((year, q)) for q in (1, 2, 3)]
        if all(p is not None for p in parts):
            by_period[(year, 4)] = total - sum(parts)  # type: ignore[arg-type]
    return by_period


def _prev(period: tuple[int, int]) -> tuple[int, int]:
    year, quarter = period
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def ttm(rows, field: str) -> tuple[int | None, str]:
    """최근 4개 **연속** 분기 합산. 불가하면 최근 사업보고서의 연간 값.

    `(값, 근거 설명)`. 둘 다 없으면 `(None, 사유)`.
    """
    quarters = quarter_values(rows, field)
    if quarters:
        last = max(quarters)
        chain = []
        cursor = last
        for _ in range(TTM_QUARTERS):
            value = quarters.get(cursor)
            if value is None:
                break
            chain.append(value)
            cursor = _prev(cursor)
        if len(chain) == TTM_QUARTERS:
            return sum(chain), f"최근 4개 분기 합산({last[0]}Q{last[1]} 기준)"
    annuals = annual_rows(rows)
    for row in reversed(annuals):
        value = _num(row.get(field))
        if value is not None:
            return value, f"{period_key(row)[0]} 사업보고서 연간 값"
    return None, "연속 4개 분기·사업보고서 어느 쪽도 없음"


def _ratio(numerator, denominator, places: int) -> Decimal | None:
    """분모가 0·음수거나 값이 없으면 None(오류 아님)."""
    if numerator is None or denominator is None:
        return None
    try:
        num = Decimal(str(numerator))
        den = Decimal(str(denominator))
    except (InvalidOperation, ValueError):
        return None
    if den <= 0:
        return None
    quant = Decimal(1).scaleb(-places)
    return (num / den).quantize(quant)


def compute_valuation(rows, *, cur_prc: int | None, shares: int | None) -> dict:
    """PER/PBR/ROE/부채비율 한 묶음.

    * `eps_ttm` : 최근 4개 분기 EPS 합산(없으면 최근 연간 EPS)
    * `bps`     : 자본총계 / 발행주식수 (가장 최근 보고서 기준)
    * `per`     : 주가 / EPS(TTM)          — EPS ≤ 0 이면 None
    * `pbr`     : 주가 / BPS               — BPS ≤ 0 이면 None
    * `roe`     : 순이익(TTM) / 자본총계 × 100 — 분자는 최근 4개 분기 **연환산이 아니라
                  실제 4개 분기 합**이므로 별도 연환산을 하지 않는다
    * `debt_ratio` : 부채총계 / 자본총계 × 100
    """
    latest = latest_row(rows)
    price = _num(cur_prc)
    if price is not None and price <= 0:
        price = None                      # 주가를 모르면 PER/PBR 도 없다(오류 아님)
    share_count = _num(shares)
    eps_ttm, eps_basis = ttm(rows, "eps")
    net_ttm, net_basis = ttm(rows, "net_profit")
    equity = _num((latest or {}).get("total_equity"))
    liabilities = _num((latest or {}).get("total_liabilities"))

    bps = None
    if equity is not None and equity > 0 and share_count and share_count > 0:
        bps = int(equity // share_count)

    return {
        "cur_prc": price,
        "eps_ttm": eps_ttm,
        "bps": bps,
        "per": _ratio(price, eps_ttm if (eps_ttm or 0) > 0 else None, 2),
        "pbr": _ratio(price, bps if (bps or 0) > 0 else None, 4),
        "roe": None if net_ttm is None else _pct(net_ttm, equity),
        "debt_ratio": _pct(liabilities, equity),
        "financial_asof": period_label(latest) if latest else "",
        "eps_basis": eps_basis,
        "net_basis": net_basis,
        "shares_outstanding": share_count,
        "net_profit_ttm": net_ttm,
        "total_equity": equity,
        "total_liabilities": liabilities,
    }


def _pct(numerator, denominator) -> Decimal | None:
    ratio = _ratio(numerator, denominator, 6)
    if ratio is None:
        return None
    return (ratio * 100).quantize(Decimal("0.0001"))
