"""시간대별 전력수요 · 재생에너지 발전량 · 기상 데이터(외부 연계 데이터) 합성 생성."""
import numpy as np
import pandas as pd

from .config import SimConfig


def generate_grid_signal(cfg: SimConfig, rng: np.random.Generator) -> pd.DataFrame:
    ts = pd.date_range(cfg.start_date, periods=cfg.n_hours, freq="h")
    hour = ts.hour.to_numpy()
    dow = ts.dayofweek.to_numpy()
    doy = ts.dayofyear.to_numpy()

    is_weekend = dow >= 5

    # 도시 전력수요(MW): 아침/저녁 이중 피크 + 주중/주말 차이 + 계절 추세 + 노이즈
    daily_shape = (
        0.55
        + 0.30 * np.exp(-((hour - 9) ** 2) / 8)
        + 0.45 * np.exp(-((hour - 19) ** 2) / 10)
    )
    weekday_factor = np.where(is_weekend, 0.85, 1.0)
    seasonal = 1.0 + 0.08 * np.sin(2 * np.pi * (doy - 30) / 365)
    noise = rng.normal(0, 0.03, size=cfg.n_hours)
    power_demand_mw = 120.0 * daily_shape * weekday_factor * seasonal * (1 + noise)

    # 재생에너지(태양광 위주) 발전량(MW): 주간 종형 곡선 + 구름/날씨 변동성
    solar_shape = np.clip(np.sin(np.pi * (hour - 6) / 12), 0, None)
    cloud_factor = np.clip(rng.normal(0.85, 0.18, size=cfg.n_hours), 0.15, 1.15)
    renewable_gen_mw = 40.0 * solar_shape * cloud_factor

    temperature_c = (
        18
        + 10 * np.sin(2 * np.pi * (doy - 105) / 365)
        + 5 * np.sin(2 * np.pi * (hour - 15) / 24)
        + rng.normal(0, 1.2, size=cfg.n_hours)
    )

    # 전력가격 프록시: 수요가 높고 재생E가 부족할수록 상승 (V2G 인센티브 신호로 사용)
    net_load = power_demand_mw - renewable_gen_mw
    price = 100 + 0.35 * (net_load - net_load.mean()) / net_load.std() * 25
    price = np.clip(price, 60, 170)

    return pd.DataFrame(
        {
            "timestamp": ts,
            "power_demand_mw": power_demand_mw.round(2),
            "renewable_gen_mw": renewable_gen_mw.round(2),
            "temperature_c": temperature_c.round(1),
            "price_krw_per_kwh": price.round(1),
            "is_holiday": is_weekend.astype(int),
        }
    )
