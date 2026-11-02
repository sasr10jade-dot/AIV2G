"""AI-V2G Closed-loop 재학습 데모 (WS5) — "예측 → 운영 → 결과측정 → 재학습" 사이클을
실제로 1회 수행한다.

절차:
  1) 기존 모델(M0)을 N주 데이터로 학습
  2) N+1주째 신규 데이터가 들어왔다고 가정하고, M0를 "재학습 없이" 그대로 적용해 오차를 측정
     → 실제 운영에서 모델이 오래될수록 오차가 벌어지는 드리프트를 재현
  3) N+1주 누적 데이터로 재학습(M1)한 뒤 같은 신규 구간에서 오차를 다시 측정
     → 재학습으로 오차가 개선되는지 확인 (이 개선폭이 곧 "재학습이 필요했다"는 근거)
"""
from dataclasses import replace

import numpy as np
from sklearn.metrics import mean_absolute_error

from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.pipeline.store import build_city_hourly
from src.model.demand_forecast import train_forecast_model, ForecastResult, TEST_HOURS
from src.model.features import build_features

MAE_DEGRADATION_TRIGGER_PCT = 20.0  # 이 이상 나빠지면 재학습 트리거


def run_closed_loop_cycle(
    base_cfg: SimConfig,
    target_col: str = "ev_charging_demand_kw",
    fleet_growth: int = 0,
    before: ForecastResult | None = None,
) -> dict:
    """fleet_growth: N+1주차에 실증 차량이 추가 투입되는 현실적 시나리오(사업 특성상 실증
    규모가 단계적으로 확대됨)를 반영하기 위한 옵션. 0이면 순수 '한 주 경과'만 반영한다.

    before: 호출자가 이미 같은 target_col로 학습해둔 "현재 모델"이 있으면(예: 대시보드가
    화면 상단 예측 적합도 카드용으로 이미 학습한 모델) 그대로 재사용한다 — cfg_before로
    데이터를 다시 생성하고 똑같은 모델을 한 번 더 학습하는 중복 연산을 피하기 위함이다.
    주어지지 않으면(단독 호출·테스트) 기존처럼 자체 생성해 학습한다."""
    cfg_before = base_cfg
    cfg_after = replace(base_cfg, weeks=base_cfg.weeks + 1, n_ev=base_cfg.n_ev + fleet_growth)

    if before is None:
        before = train_forecast_model(
            build_city_hourly(
                generate_ev_telemetry(cfg_before, np.random.default_rng(cfg_before.random_seed)),
                generate_grid_signal(cfg_before, np.random.default_rng(cfg_before.random_seed)),
                cfg_before,
            ),
            target_col,
        )

    rng = np.random.default_rng(cfg_after.random_seed)
    city_hourly_after = build_city_hourly(
        generate_ev_telemetry(cfg_after, rng), generate_grid_signal(cfg_after, rng), cfg_after
    )
    feat_after, _ = build_features(city_hourly_after, target_col)
    new_period = feat_after.iloc[-TEST_HOURS:]

    stale_pred = before.model.predict(new_period[before.feature_cols]).clip(min=0)
    stale_mae = float(mean_absolute_error(new_period[target_col], stale_pred))
    drift_pct = (stale_mae - before.mae) / before.mae * 100 if before.mae else 0.0

    after = train_forecast_model(city_hourly_after, target_col)
    improve_pct = (stale_mae - after.mae) / stale_mae * 100 if stale_mae else 0.0

    return {
        "target_col": target_col,
        "initial_mae": before.mae,
        "stale_model_mae_on_new_week": round(stale_mae, 3),
        "drift_pct": round(drift_pct, 1),
        "retrained_mae_on_new_week": after.mae,
        "improve_pct": round(improve_pct, 1),
        "retrain_recommended": drift_pct > MAE_DEGRADATION_TRIGGER_PCT,
        "trigger_threshold_pct": MAE_DEGRADATION_TRIGGER_PCT,
    }
