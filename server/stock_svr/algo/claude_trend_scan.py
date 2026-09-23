"""claude_trend_scan — 산업 트렌드 스캔(Claude 웹 검색) 진입 알고리즘.

하루 1회(`scan_time` 이후 첫 평가 사이클)만 동작하고, 그 외 사이클에서는 아무것도 하지 않는다.
실제 조사·종목 확정·기록은 `services/trend_scan.py` 가 담당하며, 이 클래스는
**다른 진입 알고리즘과 동일한 활성 조건**(자동거래 ON + 이 알고리즘이 선택됨)으로
그 서비스를 호출하는 얇은 껍데기다.

만들어진 매수 신호는 다른 진입 알고리즘과 똑같이
필터 → risk_guard → claude_advisor → Executor(주문 게이트) 를 모두 통과해야 주문된다.

파라미터(seed.sql): region_scope, model, effort, scan_time, max_domestic_themes,
max_candidates_per_theme, max_total_candidates, min_confidence, buy_amount, order_type,
max_web_searches, timeout_sec
"""
from __future__ import annotations

import logging

from ..llm.client import MODEL_OPUS, MODEL_SONNET
from ..services.trend_scan import CODE, TrendScanParams, TrendScanService
from .base import Algorithm, Signal
from .params import ParamSet
from .registry import register

log = logging.getLogger(__name__)

# 조사(웹 검색)는 품질이 중요해 Haiku 를 쓰지 않는다(seed 의 enum 과 동일)
ALLOWED_MODELS = (MODEL_OPUS, MODEL_SONNET)


@register
class ClaudeTrendScan(Algorithm):
    code = CODE
    role = "entry"
    name = "산업 트렌드 스캔(Claude)"

    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_params(params: ParamSet) -> list[str]:
        errors: list[str] = []
        model = params.str("model", MODEL_OPUS)
        if model not in ALLOWED_MODELS:
            errors.append(f"조사 모델: 허용된 값이 아닙니다 ({', '.join(ALLOWED_MODELS)})")
        if params.time("scan_time") is None:
            errors.append("조사 시각: HH:MM 형식이어야 합니다.")
        per_theme = params.int("max_candidates_per_theme", 3)
        total = params.int("max_total_candidates", 10)
        if per_theme > total:
            errors.append(f"테마당 최대 종목({per_theme})이 일 최대 후보 종목수({total})보다 큽니다.")
        return errors

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx) -> list[Signal]:
        """하루 1회 조사 + 매매 가능 시점에 신호 1회 투입(그 외에는 빈 목록).

        조사 자체는 장중 여부를 보지 않는다(조사 시각 기본 08:30 = 장 시작 전).
        그때 만든 신호는 risk_guard 의 '장 시간이 아님/매매시간 외'에 막히지 않도록
        **매매 시간에 들어설 때까지 대기**시켰다가 그날 한 번만 내보낸다.
        """
        if ctx.market is None:
            return []
        p = TrendScanParams(self.params)
        service = TrendScanService(ctx.db, market=ctx.market)
        signals = service.run_if_due(ctx, p)
        if signals:
            log.info("%s: 매수 후보 %d건", self.code, len(signals))
        return signals
