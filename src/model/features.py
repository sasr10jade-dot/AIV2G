"""AI 수요·공급 예측모델(WS2) 피처 엔지니어링 — 달력 피처 + 자기회귀(lag/rolling) 피처.

city_hourly(도시 단위 시간별 통합 뷰)를 입력으로 학습용 피처를 만든다. lag/rolling은
과거 값만 참조하므로(shift) 미래 누수가 없고, 앞부분에 생기는 결측 행은 제거한다.
"""
import numpy as np
import pandas as pd

LAG_HOURS = (1, 24, 168)  # 1시간 전, 어제 같은 시간, 지난주 같은 시간
ROLLING_WINDOWS = (24, 168)


def add_calendar_features(df: pd.DataFrame) -> pd.DataFrame:
    """시간/요일/월 등 달력 피처를 추가한다. lag/rolling과 달리 미래 시각에도 그대로
    계산 가능해(재귀 예측에서도 재사용됨) 별도 함수로 분리했다."""
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    df["hour"] = ts.dt.hour
    df["dow"] = ts.dt.dayofweek
    df["month"] = ts.dt.month
    df["is_weekend"] = (df["dow"] >= 5).astype(int)
    df["hour_sin"] = np.sin(2 * np.pi * df["hour"] / 24)
    df["hour_cos"] = np.cos(2 * np.pi * df["hour"] / 24)
    return df


def build_features(city_hourly: pd.DataFrame, target_col: str) -> tuple[pd.DataFrame, list[str]]:
    df = add_calendar_features(city_hourly.sort_values("timestamp").reset_index(drop=True))

    feature_cols = [
        "hour", "dow", "month", "is_weekend", "hour_sin", "hour_cos",
        "power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh", "is_holiday",
    ]
    feature_cols = [c for c in feature_cols if c in df.columns and c != target_col]

    for lag in LAG_HOURS:
        col = f"{target_col}_lag{lag}"
        df[col] = df[target_col].shift(lag)
        feature_cols.append(col)

    for window in ROLLING_WINDOWS:
        col = f"{target_col}_roll{window}_mean"
        df[col] = df[target_col].shift(1).rolling(window, min_periods=1).mean()
        feature_cols.append(col)

    df = df.dropna(subset=feature_cols).reset_index(drop=True)
    return df, feature_cols
