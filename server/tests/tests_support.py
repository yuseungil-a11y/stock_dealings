"""테스트 보조: 리스크 파라미터 정의, 신호 팩토리, 모의 transport REST 클라이언트."""
from __future__ import annotations

import httpx

from stock_svr.algo.base import Signal
from stock_svr.config import KiwoomConfig
from stock_svr.kiwoom.rest import KiwoomRest

RISK_DEFS = [
    {"param_key": "max_total_invest", "label": "총 투입 한도", "value_type": "int",
     "default_value": "1000000"},
    {"param_key": "max_invest_per_stock", "label": "종목당 최대 투입금", "value_type": "int",
     "default_value": "300000"},
    {"param_key": "stop_loss_pct", "label": "손절 라인", "value_type": "decimal",
     "default_value": "-15"},
    {"param_key": "daily_loss_limit_pct", "label": "일 손실 한도", "value_type": "decimal",
     "default_value": "-3"},
    {"param_key": "max_orders_per_day", "label": "일 최대 주문 횟수", "value_type": "int",
     "default_value": "30"},
    {"param_key": "trade_start_time", "label": "매매 시작 시각", "value_type": "time",
     "default_value": "09:05"},
    {"param_key": "trade_end_time", "label": "신규진입 종료 시각", "value_type": "time",
     "default_value": "15:15"},
    {"param_key": "exchange", "label": "거래소", "value_type": "enum",
     "default_value": "KRX", "enum_options": "KRX:KRX,NXT:NXT,SOR:SOR"},
]


def buy_signal(stk: str = "005930", qty: int = 10, price: int = 10000) -> Signal:
    return Signal(algo_code="momentum_screen", stk_cd=stk, stk_nm="테스트종목", side="BUY",
                  qty=qty, price=price, trde_tp="0", amount=qty * price, reason="테스트")


class _StubTokens:
    """토큰 발급을 하지 않는 대역."""

    def get_token(self, force: bool = False) -> str:
        return "TEST-TOKEN"

    def invalidate(self) -> None:
        pass


def make_rest(handler, env: str = "mock") -> KiwoomRest:
    """httpx MockTransport 로 네트워크 없이 KiwoomRest 를 만든다.

    실서버로는 단 한 건도 나가지 않는다.
    """
    cfg = KiwoomConfig(min_interval_sec_mock=0.0, min_interval_sec_real=0.0,
                       http_timeout_sec=1.0)
    rest = KiwoomRest(cfg, env, _StubTokens())
    rest._client.close()                                     # noqa: SLF001
    rest._client = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)  # noqa: SLF001
    return rest
