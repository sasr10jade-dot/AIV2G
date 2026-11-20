"""AI 기반 에너지 수요·공급 예측모델 (WS2) — EV 충전수요 · V2G 가용자원 예측.

시간순 홀드아웃(마지막 2주)으로 평가하여 재학습 루프(WS5 Closed-loop)에서
동일한 절차로 반복 평가할 수 있게 한다.

계획서 성능 목표(p.27: MAPE ≤20%, R² ≥0.70, Train/Test 80:20 분할 교차검증)는
`evaluate_plan_targets()`가 실제로 학습·평가해 산출한다(모델 카드 수치의 원천).
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from .features import build_features

TEST_HOURS = 24 * 14  # 마지막 2주를 홀드아웃으로 사용

# 계획서 p.27 AI 성능 목표
PLAN_MAPE_MAX_PCT = 20.0
PLAN_R2_MIN = 0.70
PLAN_TEST_RATIO = 0.20   # Train/Test 80:20
PLAN_CV_FOLDS = 5        # 학습 구간(80%) 안에서 시간순 확장창 교차검증
# 공고 필수성과(평가의견 11 반영 문안): 예측 정확도 = (1 − WAPE) × 100 ≥ 70% — 검증용(시험) 데이터 기준.
# MAPE·R²(계획서 p.27)는 보조 지표로 함께 보고한다.
PLAN_ACCURACY_MIN_PCT = 70.0
# 실제값이 0 근처인 시간(심야 무충전 등)은 MAPE가 무한대로 커지므로, 실제값이 평균의 10% 미만인 시간은 제외한다
MAPE_MIN_ACTUAL_RATIO = 0.10

LGBM_PARAMS = dict(
    n_estimators=800,
    learning_rate=0.04,
    max_depth=6,
    num_leaves=31,
    min_child_samples=15,
    reg_lambda=0.5,
    random_state=42,
    verbosity=-1,
)


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
    mape: float | None = None  # 평균 절대 백분율 오차(%) — 실제값이 작은 시간 제외(MAPE_MIN_ACTUAL_RATIO)
    r2: float | None = None    # 결정계수


def wape(y_true, y_pred) -> float:
    denom = float(np.abs(y_true).sum())
    return float(np.abs(np.asarray(y_pred) - np.asarray(y_true)).sum() / denom * 100) if denom > 0 else 0.0


def mape(y_true, y_pred, min_actual_ratio: float = MAPE_MIN_ACTUAL_RATIO) -> float | None:
    """MAPE(%) — 실제값이 평균의 min_actual_ratio 미만인 시점은 분모가 0에 가까워 제외한다. 남는 시점이 없으면 None."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    floor = float(np.abs(y_true).mean()) * min_actual_ratio
    mask = np.abs(y_true) > max(floor, 1e-9)
    if not mask.any():
        return None
    return float(np.mean(np.abs(y_pred[mask] - y_true[mask]) / np.abs(y_true[mask])) * 100)


def _r2(y_true, y_pred) -> float | None:
    return float(r2_score(y_true, y_pred)) if len(y_true) >= 2 and float(np.var(y_true)) > 0 else None


def _round(v, nd):
    return None if v is None else round(v, nd)


def _fit_lgbm(train_df: pd.DataFrame, feature_cols: list[str], target_col: str, val_hours: int) -> lgb.LGBMRegressor:
    """학습 구간 마지막 부분을 시간순 검증셋으로 떼어 조기종료한다(과적합 방지)."""
    val_split = max(int(len(train_df) * 0.9), len(train_df) - val_hours)
    fit_df, val_df = train_df.iloc[:val_split], train_df.iloc[val_split:]
    model = lgb.LGBMRegressor(**LGBM_PARAMS)
    model.fit(
        fit_df[feature_cols], fit_df[target_col],
        eval_X=val_df[feature_cols], eval_y=val_df[target_col],
        callbacks=[lgb.early_stopping(stopping_rounds=30, verbose=False)],
    )
    return model


def _metrics(y_true, y_pred) -> dict:
    return {
        "mae": round(mean_absolute_error(y_true, y_pred), 3),
        "rmse": round(mean_squared_error(y_true, y_pred) ** 0.5, 3),
        "wape_pct": round(wape(y_true, y_pred), 2),
        "accuracy_pct": round(max(0.0, 100.0 - wape(y_true, y_pred)), 2),   # 예측 정확도 = (1 − WAPE) × 100
        "mape_pct": _round(mape(y_true, y_pred), 2),
        "r2": _round(_r2(y_true, y_pred), 3),
    }


def evaluate_plan_targets(city_hourly: pd.DataFrame, target_col: str,
                          test_ratio: float = PLAN_TEST_RATIO, cv_folds: int = PLAN_CV_FOLDS) -> dict:
    """계획서 p.27 기준 성능 평가 — 시간순 80:20 분할 + 학습 구간 시간순 교차검증.

    - test: 마지막 20%를 학습에 한 번도 쓰지 않은 시험 구간으로 두고 MAPE·R² 등을 계산한다.
    - cv: 앞 80% 안에서 확장창(expanding window) 방식으로 cv_folds번 학습·검증해 평균·편차를 낸다
      (시계열은 무작위로 섞으면 미래 정보가 새므로 시간순으로만 나눈다).
    - 목표 판정: 시험 구간 MAPE ≤ 20%, R² ≥ 0.70.
    """
    feat_df, feature_cols = build_features(city_hourly, target_col)
    n = len(feat_df)
    split_idx = int(round(n * (1 - test_ratio)))
    train_df, test_df = feat_df.iloc[:split_idx], feat_df.iloc[split_idx:]

    model = _fit_lgbm(train_df, feature_cols, target_col, max(24, len(test_df) // 4))
    y_true = test_df[target_col].to_numpy()
    test_metrics = _metrics(y_true, model.predict(test_df[feature_cols]).clip(min=0))

    folds = []
    fold_size = len(train_df) // (cv_folds + 1)
    for k in range(1, cv_folds + 1):
        tr = train_df.iloc[: fold_size * k]
        va = train_df.iloc[fold_size * k: fold_size * (k + 1)]
        if len(tr) < 48 or len(va) < 24:
            continue
        m = _fit_lgbm(tr, feature_cols, target_col, max(24, len(va) // 4))
        p = m.predict(va[feature_cols]).clip(min=0)
        folds.append({"fold": k, "n_train": len(tr), "n_valid": len(va), **_metrics(va[target_col].to_numpy(), p)})

    def _agg(key):
        vals = [f[key] for f in folds if f.get(key) is not None]
        return {"mean": round(float(np.mean(vals)), 3), "std": round(float(np.std(vals)), 3)} if vals else None

    mape_ok = test_metrics["mape_pct"] is not None and test_metrics["mape_pct"] <= PLAN_MAPE_MAX_PCT
    r2_ok = test_metrics["r2"] is not None and test_metrics["r2"] >= PLAN_R2_MIN
    acc_ok = test_metrics["accuracy_pct"] >= PLAN_ACCURACY_MIN_PCT
    params = {k: v for k, v in LGBM_PARAMS.items() if k != "verbosity"}
    params["best_iteration"] = int(model.best_iteration_) if getattr(model, "best_iteration_", None) else None
    return {
        "target_col": target_col,
        "method": f"시간순 Train/Test {round((1 - test_ratio) * 100)}:{round(test_ratio * 100)} 분할 + 학습 구간 {len(folds)}겹 시간순 교차검증",
        "n_rows": n,
        "n_train": len(train_df),
        "n_test": len(test_df),
        "feature_cols": feature_cols,
        "hyperparameters": params,
        "data_range": {
            "train_start": pd.Timestamp(train_df["timestamp"].min()).isoformat(),
            "train_end": pd.Timestamp(train_df["timestamp"].max()).isoformat(),
            "test_start": pd.Timestamp(test_df["timestamp"].min()).isoformat(),
            "test_end": pd.Timestamp(test_df["timestamp"].max()).isoformat(),
        },
        "test": test_metrics,
        "cv": {"folds": folds, "mape_pct": _agg("mape_pct"), "r2": _agg("r2"), "wape_pct": _agg("wape_pct"),
               "accuracy_pct": _agg("accuracy_pct")},
        "targets": {
            "mape_max_pct": PLAN_MAPE_MAX_PCT, "r2_min": PLAN_R2_MIN,
            "mape_met": mape_ok, "r2_met": r2_ok, "all_met": mape_ok and r2_ok,
            # 공고 필수성과 판정(주 지표): 예측 정확도 ≥ 70%
            "accuracy_min_pct": PLAN_ACCURACY_MIN_PCT, "accuracy_met": acc_ok,
            "accuracy_formula": "예측 정확도 = (1 − WAPE) × 100, WAPE = Σ|예측−실제| ÷ Σ|실제|",
        },
        "mape_note": f"실제값이 평균의 {int(MAPE_MIN_ACTUAL_RATIO * 100)}% 미만인 시간은 MAPE 계산에서 제외(분모 0 근처 왜곡 방지)",
    }


def _smape(y_true, y_pred) -> float:
    denom = (np.abs(y_true) + np.abs(y_pred)).clip(min=1e-6)
    return float(np.mean(2 * np.abs(y_pred - y_true) / denom) * 100)


def train_forecast_model(city_hourly: pd.DataFrame, target_col: str) -> ForecastResult:
    feat_df, feature_cols = build_features(city_hourly, target_col)

    split_idx = len(feat_df) - TEST_HOURS
    train_df, test_df = feat_df.iloc[:split_idx], feat_df.iloc[split_idx:]

    # 학습 구간 마지막 10%를 시간순 검증셋으로 분리해 조기종료 — 과적합 방지
    model = _fit_lgbm(train_df, feature_cols, target_col, TEST_HOURS)

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
        mape=_round(mape(y_true, y_pred), 2),
        r2=_round(_r2(y_true, y_pred), 3),
        data_range={
            "train_start": pd.Timestamp(train_df["timestamp"].min()).isoformat(),
            "train_end": pd.Timestamp(train_df["timestamp"].max()).isoformat(),
            "test_start": pd.Timestamp(test_df["timestamp"].min()).isoformat(),
            "test_end": pd.Timestamp(test_df["timestamp"].max()).isoformat(),
        },
    )
