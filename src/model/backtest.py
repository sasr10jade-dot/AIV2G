"""수익 기반 백테스트 — 가격 예측이 "얼마나 맞았나"가 아니라 "그 예측으로 짠 스케줄이 실제
가격에서 얼마를 벌었나"로 예측 품질을 평가한다.

비교 3종 (같은 세션 조건, 같은 경제성 가정):
- forecast: 예측가격으로 스케줄을 만들고 **실제 가격**으로 정산
- oracle:   실제 가격을 미리 아는 이상적 스케줄(수익 상한)
- naive:    전날 같은 시각 가격을 예측으로 쓴 스케줄(지속성 기준선)
capture_ratio = forecast 순이익 / oracle 순이익 — 예측이 상한의 몇 %를 회수했는지.
"""
from __future__ import annotations

import pandas as pd

from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import _single_vehicle_arbitrage_schedule


def settle(schedule: dict, price_by_time: dict, eco: EconomicsParams) -> float:
    """스케줄(시간별 (충전kWh, 방전kWh))을 주어진 가격으로 정산한 순이익(원)."""
    net = 0.0
    for t, (c, d) in schedule.items():
        p = float(price_by_time.get(t, 0.0))
        net += d * p - c * p - d / eco.discharge_efficiency * eco.degradation_krw_per_kwh
    return net


def revenue_backtest(
    times: list[pd.Timestamp],
    actual_price: dict,
    forecast_price: dict,
    eco: EconomicsParams,
    needed_kwh: float = 0.0,
    max_net_kwh: float = 40.0,
    headroom_kwh: float = 40.0,
    max_kw: float = 7.0,
) -> dict:
    def plan(prices: dict) -> dict:
        return _single_vehicle_arbitrage_schedule(
            times, prices, needed_kwh, max_net_kwh, headroom_kwh, max_kw, max_kw, True, eco
        )

    naive_price = {t: actual_price.get(t - pd.Timedelta(days=1), actual_price.get(t, 0.0)) for t in times}
    forecast_net = settle(plan(forecast_price), actual_price, eco)
    oracle_net = settle(plan(actual_price), actual_price, eco)
    naive_net = settle(plan(naive_price), actual_price, eco)
    return {
        "forecast_net_krw": round(forecast_net, 0),
        "oracle_net_krw": round(oracle_net, 0),
        "naive_net_krw": round(naive_net, 0),
        "capture_ratio": round(forecast_net / oracle_net, 3) if oracle_net > 0 else None,
        "beats_naive": forecast_net >= naive_net,
    }
