"""알고리즘 공통 인터페이스.

새 알고리즘은 `Algorithm` 을 상속하고 `code` 를 `algorithm.code` 와 동일하게 두면
레지스트리에 자동 등록되어 UI 에 노출된다(+ DB 에 `algorithm`/`algorithm_param_def` 행 추가).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .params import ParamSet

if TYPE_CHECKING:  # pragma: no cover
    from ..engine.context import EngineContext

# 시장가/최유리 주문의 한도 검사·사이클 누적에 적용할 슬리피지 버퍼 (S-12 / R-12)
SLIPPAGE_BUFFER = Decimal("1.1")

# 신호 종류
KIND_ENTRY = "entry"
KIND_AVG_DOWN = "avg_down"
KIND_STOP_LOSS = "stop_loss"
KIND_LIQUIDATE = "liquidate"
KIND_TAKE_PROFIT = "take_profit"


@dataclass
class Signal:
    """알고리즘이 만들어내는 매매 신호. 주문 여부는 Executor 의 게이트가 결정한다."""

    algo_code: str
    stk_cd: str
    side: str                      # 'BUY' | 'SELL'
    qty: int = 0
    price: int | None = None       # 지정가일 때만. 시장가는 None
    trde_tp: str = "3"             # 0=보통(지정가) 3=시장가 6=최유리
    amount: int | None = None      # 의도한 투입금액(원)
    reason: str = ""
    stk_nm: str | None = None
    score: float | None = None
    kind: str = KIND_ENTRY
    exchange: str = "KRX"
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"

    @property
    def est_amount(self) -> int:
        if self.amount is not None:
            return int(self.amount)
        if self.price and self.qty:
            return int(self.price) * int(self.qty)
        return 0

    def __str__(self) -> str:
        return (f"{self.algo_code}/{self.kind} {self.side} {self.stk_cd}"
                f"{'(' + self.stk_nm + ')' if self.stk_nm else ''} x{self.qty}"
                f"{'@' + str(self.price) if self.price else '@시장가'} : {self.reason}")


class Algorithm:
    """알고리즘 기반 클래스."""

    code: str = ""
    role: str = "entry"       # entry | risk | filter
    name: str = ""

    def __init__(self, meta: dict | None = None, params: ParamSet | None = None):
        self.meta = meta or {}
        self.params = params or ParamSet([], {})
        self.name = self.meta.get("name") or self.name or self.code

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx: "EngineContext") -> list[Signal]:
        """평가 주기마다 호출되어 신호 목록을 반환한다(기본: 없음)."""
        return []

    def filter_signals(self, ctx: "EngineContext", signals: list[Signal]) -> list[Signal]:
        """role='filter' 알고리즘이 신호를 걸러낸다(기본: 통과)."""
        return signals

    # ------------------------------------------------------------------ #
    @property
    def priority(self) -> int:
        return int(self.meta.get("priority", self.meta.get("sort_order", 100)) or 100)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} code={self.code} role={self.role}>"


def amount_with_buffer(signal: Signal) -> int:
    """한도 검사·사이클 누적에 쓰는 보수적 주문금액 (S-12 / R-12).

    지정가(trde_tp='0') 외에는 체결가가 오를 수 있으므로 슬리피지 버퍼를 얹는다.
    risk_guard 의 사전 검증과 Executor 의 사후 누적이 **같은 금액**을 쓰도록 여기 한 곳에 둔다.
    """
    amount = signal.est_amount
    if amount <= 0:
        return 0
    if signal.trde_tp != "0":
        return int(Decimal(amount) * SLIPPAGE_BUFFER)
    return amount


def qty_for_amount(amount: int, price: int | None, min_qty: int = 1) -> int:
    """투입금액과 현재가로 주문수량 산출."""
    if not price or price <= 0:
        return 0
    qty = int(amount // price)
    return qty if qty >= min_qty else 0
