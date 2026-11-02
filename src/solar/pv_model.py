"""태양광 발전량 모델 — 태양 위치로 맑은 날 일사량을 구하고, 날씨·기온 영향을 곱해 시간별 발전량(kW)을 낸다.

외부 라이브러리(pvlib) 없이 numpy/pandas만 쓴다. 단순화한 모델이라 정밀 설계용이 아니라 **충전 시간·자가소비
시나리오를 비교하기 위한 에뮬레이션용**이다.

계산 흐름
1) 태양 고도: 적위(Cooper)·균시차·경도 보정으로 시간별 cos(천정각)
2) 맑은 날 수평면 일사량(GHI): Haurwitz  GHI = 1098·cosZ·exp(-0.059/cosZ)
3) 날씨 계수(sky factor): 날짜마다 맑음/구름조금/흐림/비 중 하나를 뽑고 시간별 변동을 더함. 같은 (시드, 날짜)는
   조회 기간이 달라도 항상 같은 날씨가 나온다(재현성).
4) 경사면 일사량 = GHI × 경사 이득, 셀 온도 = 기온 + 0.031 × 경사면 일사량
5) 출력 = kWp × (경사면 일사량/1000) × 성능비 × (1 + 온도계수 × (셀온도-25)), 0~kWp로 제한

**가정**: 날씨 확률·계수·기온 곡선은 한반도 호남 지역을 근사한 임의값이다. 실제 관측(기상청 일사, OD-05)이나
전력거래소 지역 발전량(OD-03)이 있으면 `sky_factor_from_ghi`로 날씨 계수를 대체한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.solar.site import SolarSite

HAURWITZ_A = 1098.0
HAURWITZ_B = 0.059
CELL_TEMP_RISE_PER_WM2 = 0.031          # (NOCT 45℃ - 20℃) / 800 W/m²
MJ_PER_M2_HOUR_TO_WM2 = 1e6 / 3600.0    # 기상청 일사 MJ/m²(시간 적산) → 평균 W/m²

# (이름, 확률, 맑은 날 대비 일사 계수, 시간별 변동 표준편차) — 호남 지역 근사 가정
SKY_STATES = (
    ("clear", 0.38, 0.95, 0.03),
    ("partly", 0.30, 0.70, 0.18),
    ("cloudy", 0.20, 0.40, 0.12),
    ("rain", 0.12, 0.15, 0.08),
)
CLIMATOLOGY_SKY_FACTOR = sum(p * f for _n, p, f, _s in SKY_STATES)   # 날씨를 모를 때 쓰는 평균 계수


def _local_index(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        raise ValueError("시각 인덱스는 현지시각(tz-naive)이어야 합니다")
    return idx


def cos_zenith(index, site: SolarSite) -> np.ndarray:
    """각 시간의 중간 시점(+30분) 기준 cos(천정각). 해가 지평선 아래면 음수."""
    t = _local_index(index) + pd.Timedelta(minutes=30)
    doy = np.asarray(t.dayofyear, dtype=float)
    hour = np.asarray(t.hour, dtype=float) + np.asarray(t.minute, dtype=float) / 60.0
    b = 2 * np.pi * (doy - 81) / 364.0
    eot_min = 9.87 * np.sin(2 * b) - 7.53 * np.cos(b) - 1.5 * np.sin(b)
    standard_meridian = site.tz_offset_h * 15.0
    solar_time = hour + (4.0 * (site.lon - standard_meridian) + eot_min) / 60.0
    hour_angle = np.radians(15.0 * (solar_time - 12.0))
    decl = np.radians(23.45) * np.sin(2 * np.pi * (284 + doy) / 365.0)
    lat = np.radians(site.lat)
    return np.sin(lat) * np.sin(decl) + np.cos(lat) * np.cos(decl) * np.cos(hour_angle)


def clear_sky_ghi(index, site: SolarSite) -> pd.Series:
    """맑은 날 수평면 전일사량(W/m²). 밤에는 0."""
    idx = _local_index(index)
    cz = cos_zenith(idx, site)
    safe = np.where(cz > 0.01, cz, 1.0)          # 지평선 근처 0 나눗셈 방지(그 구간은 아래에서 0 처리)
    ghi = np.where(cz > 0.01, HAURWITZ_A * safe * np.exp(-HAURWITZ_B / safe), 0.0)
    return pd.Series(ghi, index=idx, name="clear_ghi_wm2")


def ambient_temp_c(index) -> pd.Series:
    """기온(℃) 근사 — 연평균 13.5℃, 연교차 ±12℃(7월 말 최고), 일교차 ±5℃(15시 최고). 호남 지역 근사 가정."""
    idx = _local_index(index)
    doy = np.asarray(idx.dayofyear, dtype=float)
    hour = np.asarray(idx.hour, dtype=float) + 0.5
    temp = 13.5 + 12.0 * np.sin(2 * np.pi * (doy - 115) / 365.0) + 5.0 * np.sin(2 * np.pi * (hour - 9) / 24.0)
    return pd.Series(temp, index=idx, name="ambient_c")


def sky_state_for_day(day: pd.Timestamp, seed: int) -> tuple[str, float, float]:
    """그날의 날씨 상태(이름, 일사 계수, 변동 표준편차). (시드, 날짜)만으로 결정된다."""
    rng = np.random.default_rng([seed, day.toordinal(), 1])
    probs = np.array([s[1] for s in SKY_STATES])
    k = int(rng.choice(len(SKY_STATES), p=probs / probs.sum()))
    name, _p, factor, sd = SKY_STATES[k]
    return name, factor, sd


def sky_factor_series(index, seed: int = 0) -> pd.Series:
    """시간별 날씨 계수(0~1). 같은 (시드, 날짜)는 조회 기간과 무관하게 같은 값이다."""
    idx = _local_index(index)
    values = np.empty(len(idx))
    days = idx.normalize()
    cache: dict[pd.Timestamp, tuple[str, float, float, np.ndarray]] = {}
    for i, (ts, day) in enumerate(zip(idx, days)):
        if day not in cache:
            _name, factor, sd = sky_state_for_day(day, seed)
            noise = np.random.default_rng([seed, day.toordinal(), 2]).normal(0.0, sd, 24)
            cache[day] = (_name, factor, sd, noise)
        _n, factor, _sd, noise = cache[day]
        values[i] = np.clip(factor * (1.0 + noise[ts.hour]), 0.05, 1.0)
    return pd.Series(values, index=idx, name="sky_factor")


def sky_label_series(index, seed: int = 0) -> pd.Series:
    """날짜별 날씨 이름(clear/partly/cloudy/rain) — 화면 표시용."""
    idx = _local_index(index)
    return pd.Series([sky_state_for_day(d, seed)[0] for d in idx.normalize()], index=idx, name="sky")


def sky_factor_from_ghi(observed_ghi_wm2, index, site: SolarSite) -> pd.Series:
    """실측 일사(W/m²)로 날씨 계수를 만든다 — 기상청 관측(OD-05)으로 교체할 때 쓰는 연결점.
    MJ/m² 시간 적산값이면 먼저 `MJ_PER_M2_HOUR_TO_WM2`를 곱한다."""
    idx = _local_index(index)
    clear = clear_sky_ghi(idx, site).to_numpy()
    obs = np.asarray(observed_ghi_wm2, dtype=float)
    if obs.shape != clear.shape:
        raise ValueError("관측 일사와 시각 인덱스 길이가 다릅니다")
    ratio = np.divide(obs, clear, out=np.zeros_like(obs), where=clear > 1.0)
    return pd.Series(np.clip(ratio, 0.0, 1.1), index=idx, name="sky_factor")


def pv_power_kw(index, site: SolarSite, sky_factor: pd.Series | float | None = None,
                ambient_c: pd.Series | None = None, seed: int = 0) -> pd.Series:
    """시간별 태양광 발전량(kW, 해당 시간 평균). sky_factor를 안 주면 시드 기반 날씨를 쓴다."""
    idx = _local_index(index)
    if sky_factor is None:
        sky = sky_factor_series(idx, seed).to_numpy()
    elif isinstance(sky_factor, (int, float)):
        sky = np.full(len(idx), float(sky_factor))
    else:
        sky = np.asarray(sky_factor, dtype=float)
    amb = (ambient_c if ambient_c is not None else ambient_temp_c(idx)).to_numpy()
    poa = clear_sky_ghi(idx, site).to_numpy() * sky * site.poa_gain
    cell = amb + CELL_TEMP_RISE_PER_WM2 * poa
    power = site.kwp * (poa / 1000.0) * site.performance_ratio * (1.0 + site.temp_coeff_per_c * (cell - 25.0))
    return pd.Series(np.clip(power, 0.0, site.kwp), index=idx, name="pv_kw")


def expected_pv_day(day, site: SolarSite) -> pd.Series:
    """날씨를 모를 때(예: 모레 이후) 쓰는 평균 날씨 기준 하루 발전량(kW) — 추천·안내용 근사."""
    d = pd.Timestamp(day).normalize()
    idx = pd.date_range(d, periods=24, freq="h")
    return pv_power_kw(idx, site, sky_factor=CLIMATOLOGY_SKY_FACTOR)
