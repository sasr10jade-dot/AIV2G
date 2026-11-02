"""예측 서빙 계층 (Serving Layer) — 레지스트리에 등록된 활성 모델로 예측을 수행한다.

`src/pipeline/source.py`를 통해 업로드된 실제 데이터가 있으면 그것을, 없으면 동일
SimConfig로 생성한 데모 시나리오를 피처 원천으로 쓴다 — 예측·최적화 로직은 데이터
출처를 신경 쓰지 않는다.
"""
import math
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from src.automation.model_registry import ModelRegistry
from src.datagen.config import SimConfig
from src.datagen.grid import generate_grid_signal
from src.model.features import add_calendar_features, build_features
from src.pipeline.source import load_scenario
from src.pipeline.store import build_city_hourly

GRANULARITY_PRESETS = {
    # 일별(시간대별): 다음 48시간을 시간 단위 그대로 — 단기 운영 대응용
    "hourly": {"horizon_hours": 48, "bucket": "hour", "label": "일별(시간대별)"},
    # 주별: 다음 4주를 하루 합계(kWh) 단위로 — 주간 계획용
    "daily": {"horizon_hours": 24 * 28, "bucket": "day", "label": "주별"},
    # 월별: 다음 약 6개월을 주 합계(kWh) 단위로 — 중기 추세 전망용
    "weekly": {"horizon_hours": 24 * 180, "bucket": "week", "label": "월별"},
}
EXOGENOUS_COLS = ["power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh", "is_holiday"]


@dataclass
class ForecastResponse:
    target_col: str
    model_version: int
    is_real_data: bool
    timestamps: list[str]
    y_actual: list[float]
    y_pred: list[float]


def forecast_latest(target_col: str, cfg: SimConfig | None = None, periods: int = 24) -> ForecastResponse:
    cfg = cfg or SimConfig()
    registry = ModelRegistry(target_col)
    model, feature_cols, info = registry.load()

    ev_df, _charger_df, grid_df, is_real = load_scenario(cfg)
    city_hourly = build_city_hourly(ev_df, grid_df, cfg)
    feat_df, _ = build_features(city_hourly, target_col)
    recent = feat_df.iloc[-periods:]

    y_pred = model.predict(recent[feature_cols]).clip(min=0)
    return ForecastResponse(
        target_col=target_col,
        model_version=info["version"],
        is_real_data=is_real,
        timestamps=[t.isoformat() for t in recent["timestamp"]],
        y_actual=[round(float(v), 3) for v in recent[target_col]],
        y_pred=[round(float(v), 3) for v in y_pred],
    )


def _bucket_series(df: pd.DataFrame, target_col: str, bucket: str) -> pd.DataFrame:
    if bucket == "hour":
        return df.reset_index(drop=True)
    freq = {"day": "1D", "week": "1W"}[bucket]
    return df.set_index("timestamp")[[target_col]].resample(freq).sum().reset_index()


def _bucket_band(band_hourly: np.ndarray, timestamps: pd.Series, bucket: str) -> list[float]:
    """시간당 오차 밴드를 일/주 합계 단위로 옮길 때는 단순 합이 아니라 제곱합의
    제곱근(오차가 서로 독립이라고 가정한 표준적인 오차 전파)을 쓴다 — 그래야 "월별"처럼
    많은 시간을 더할수록 밴드가 비현실적으로 커지는 과대추정을 피한다."""
    if bucket == "hour":
        return [round(float(b), 2) for b in band_hourly]
    freq = {"day": "1D", "week": "1W"}[bucket]
    df = pd.DataFrame({"timestamp": timestamps.to_numpy(), "band_sq": band_hourly ** 2})
    agg = df.set_index("timestamp")["band_sq"].resample(freq).sum()
    return [round(float(v ** 0.5), 2) for v in agg]


def forecast_horizon(target_col: str, granularity: str, cfg: SimConfig | None = None) -> dict:
    """재귀 예측(recursive forecasting) — 모델 자신의 이전 예측값을 다음 시점의
    lag/rolling 피처로 다시 먹여 미래로 걸어나간다.

    외생변수(계통신호)는 실제로는 미래에 알 수 없으므로, 이 데모에서는 같은 계절·요일
    패턴이 이어진다고 가정하고 합성 생성기를 그대로 연장해 채운다(기후값 방식 근사) —
    타깃 자신의 시계열 동역학만 AI가 재귀적으로 예측하는 것이 이 기능의 핵심이다.
    기간이 길어질수록(특히 '월별') 오차가 누적되므로 정밀 예측이 아니라 추세 전망으로
    다룬다.
    """
    if granularity not in GRANULARITY_PRESETS:
        raise ValueError(f"알 수 없는 조회 단위: {granularity}")
    preset = GRANULARITY_PRESETS[granularity]
    horizon_hours, bucket = preset["horizon_hours"], preset["bucket"]

    cfg = cfg or SimConfig()
    registry = ModelRegistry(target_col)
    model, feature_cols, info = registry.load()

    ev_df, _charger_df, grid_df, is_real = load_scenario(cfg)
    city_hourly = build_city_hourly(ev_df, grid_df, cfg).sort_values("timestamp").reset_index(drop=True)
    n_hist = len(city_hourly)

    last_ts = city_hourly["timestamp"].max()
    extra_weeks = math.ceil((horizon_hours + 24) / (7 * 24)) + 1
    ext_cfg = replace(cfg, weeks=cfg.weeks + extra_weeks)
    ext_grid = generate_grid_signal(ext_cfg, np.random.default_rng(cfg.random_seed))
    future_grid = ext_grid[ext_grid["timestamp"] > last_ts].head(horizon_hours).reset_index(drop=True)

    future = future_grid[["timestamp"] + [c for c in EXOGENOUS_COLS if c in future_grid.columns]].copy()
    future[target_col] = np.nan

    combined = pd.concat([city_hourly[["timestamp", target_col] + EXOGENOUS_COLS], future], ignore_index=True)
    combined = add_calendar_features(combined)
    target = combined[target_col].to_numpy(dtype=float, copy=True)

    for i in range(n_hist, len(combined)):
        row = {col: combined.at[i, col] for col in feature_cols if col in combined.columns}
        row[f"{target_col}_lag1"] = target[i - 1]
        row[f"{target_col}_lag24"] = target[i - 24]
        row[f"{target_col}_lag168"] = target[i - 168]
        row[f"{target_col}_roll24_mean"] = float(np.mean(target[i - 24 : i]))
        row[f"{target_col}_roll168_mean"] = float(np.mean(target[i - 168 : i]))

        x = pd.DataFrame([row])[feature_cols]
        target[i] = max(float(model.predict(x)[0]), 0.0)

    combined[target_col] = target
    forecast_df = combined.iloc[n_hist:][["timestamp", target_col]]
    bucketed = _bucket_series(forecast_df, target_col, bucket)

    # 재귀 예측은 한 걸음 앞설 때마다(자기 예측을 다시 입력으로 먹으므로) 오차가
    # 누적된다 — 정확한 확률적 예측구간이 아니라, 홀드아웃 MAE(1스텝 오차)가
    # 걸음 수의 제곱근에 비례해 커진다는 표준적인 랜덤워크형 근사를 밴드로 보여준다.
    # 목적은 "월별처럼 멀리 볼수록 불확실하다"를 텍스트 각주가 아니라 눈으로
    # 보여주는 것이지, 통계적으로 보증된 신뢰구간이 아니다.
    steps = np.arange(1, horizon_hours + 1)
    band_hourly = float(info["holdout_mae"]) * np.sqrt(steps)
    band_bucketed = _bucket_band(band_hourly, forecast_df["timestamp"], bucket)

    values = [round(float(v), 2) for v in bucketed[target_col]]
    values_upper = [round(v + b, 2) for v, b in zip(values, band_bucketed)]
    values_lower = [round(max(v - b, 0.0), 2) for v, b in zip(values, band_bucketed)]

    return {
        "target_col": target_col,
        "granularity": granularity,
        "label": preset["label"],
        "bucket": bucket,
        "model_version": info["version"],
        "is_real_data": is_real,
        "timestamps": [t.isoformat() for t in bucketed["timestamp"]],
        "values": values,
        "values_lower": values_lower,
        "values_upper": values_upper,
    }
