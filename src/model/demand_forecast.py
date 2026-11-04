"""AI 기반 에너지 수요·공급 예측모델 (WS2) — EV 충전수요 · V2G 가용자원 예측.

시간순 홀드아웃(마지막 2주)으로 평가하여 재학습 루프(WS5 Closed-loop)에서
동일한 절차로 반복 평가할 수 있게 한다.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import mean_absolute_error, mean_squared_error

from .features import build_features

TEST_HOURS = 24 * 14  # 마지막 2주를 홀드아웃으로 사용


@dataclass
class ForecastResult:
    target_col: str
    model: lgb.LGBMRegressor
    feature_cols: list[str]
    mae: float
    rmse: float
    smape: float
    n_train: int
    n_test: int
    test_df: pd.DataFrame  # timestamp, y_true, y_pred
    wape: float = 0.0  # 가중 절대 오차율(%) = sum|오차| / sum|실제| — 규모가 다른 구간을 공정하게 비교
    data_range: dict | None = None  # 학습·검증 구간(시작~끝 시각) — 계획서 6장 "학습 데이터 범위를 기록해 재현"


def wape(y_true, y_pred) -> float:
    denom = float(np.abs(y_true).sum())
    return float(np.abs(np.asarray(y_pred) - np.asarray(y_true)).sum() / denom * 100) if denom > 0 else 0.0


def _smape(y_true, y_pred) -> float:
    denom = (np.abs(y_true) + np.abs(y_pred)).clip(min=1e-6)
    return float(np.mean(2 * np.abs(y_pred - y_true) / denom) * 100)


def train_forecast_model(city_hourly: pd.DataFrame, target_col: str) -> ForecastResult:
    feat_df, feature_cols = build_features(city_hourly, target_col)

    split_idx = len(feat_df) - TEST_HOURS
    train_df, test_df = feat_df.iloc[:split_idx], feat_df.iloc[split_idx:]

    # 학습 구간 마지막 10%를 시간순 검증셋으로 분리해 조기종료 — 과적합 방지
    val_split = max(int(len(train_df) * 0.9), len(train_df) - TEST_HOURS)
    fit_df, val_df = train_df.iloc[:val_split], train_df.iloc[val_split:]

    model = lgb.LGBMRegressor(
        n_estimators=800,
        learning_rate=0.04,
        max_depth=6,
        num_leaves=31,
        min_child_samples=15,
        reg_lambda=0.5,
        random_state=42,
        verbosity=-1,
    )
    model.fit(
        fit_df[feature_cols], fit_df[target_col],
        eval_X=val_df[feature_cols], eval_y=val_df[target_col],
        callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)],
    )

    y_pred = model.predict(test_df[feature_cols]).clip(min=0)
    y_true = test_df[target_col].to_numpy()

    result_df = pd.DataFrame(
        {"timestamp": test_df["timestamp"].to_numpy(), "y_true": y_true, "y_pred": y_pred}
    )

    return ForecastResult(
        target_col=target_col,
        model=model,
        feature_cols=feature_cols,
        mae=round(mean_absolute_error(y_true, y_pred), 3),
        rmse=round(mean_squared_error(y_true, y_pred) ** 0.5, 3),
        smape=round(_smape(y_true, y_pred), 2),
        n_train=len(train_df),
        n_test=len(test_df),
        test_df=result_df,
        wape=round(wape(y_true, y_pred), 2),
        data_range={
            "train_start": pd.Timestamp(train_df["timestamp"].min()).isoformat(),
            "train_end": pd.Timestamp(train_df["timestamp"].max()).isoformat(),
            "test_start": pd.Timestamp(test_df["timestamp"].min()).isoformat(),
            "test_end": pd.Timestamp(test_df["timestamp"].max()).isoformat(),
        },
    )
