"""전력거래소 지역별 시간별 태양광 발전량(공공데이터 15103243)으로 나주 모델의 날씨 계수를 보정한다.

**데이터의 성격(공식 페이지 기준)**: 광역시·도 단위(나주가 아니라 전남 전체)이고, 전력시장에 등록된 사업자의 발전량(MWh)이다.
나주 개별 설비의 발전량이 아니라 "그 시간에 전남 태양광이 얼마나 나왔나"라는 날씨 영향의 지표로 쓴다.

**보정 방법**: 전남 총발전량은 설비 용량(MW)을 모르므로 절대값을 쓰지 않고 정규화한다.
1) 시간별 비율 r = 전남 발전량 / 모델의 맑은 날 일사량
2) 맑은 날 기준 scale = r의 95퍼센타일(관측 구간 안에서 가장 맑았던 때)
3) 날씨 계수 = clip(r / scale × 0.95, 0, 1) — 모델의 '맑음' 계수(0.95)와 같은 눈금으로 맞춘다
관측 구간에 맑은 날이 한 번도 없으면 scale이 낮게 잡혀 계수가 과대평가된다 → 최소 일수를 요구한다. 전남 전체의 구름 상태가
나주와 같다고 가정하는 근사이며(지역 내 편차 무시), 용량이 구간 중에 늘어나는 효과는 보정하지 못한다.
"""
from __future__ import annotations

from datetime import date as date_type
from typing import Callable

import numpy as np
import pandas as pd

from src.pipeline import public_api
from src.solar import pv_model
from src.solar.site import SolarSite

REGION_KEYWORDS = ("전라남도", "전남")
MIN_CLEAR_GHI_WM2 = 50.0       # 이보다 해가 낮은 시간은 비율이 불안정해 제외
CLEAR_REFERENCE = 0.95         # pv_model의 '맑음' 계수와 같은 눈금
MAX_FETCH_DAYS = 90            # 개발계정 하루 100회 제한을 고려한 상한(하루 1회 호출 기준)
MIN_VALID_DAYS = 7


class CalibrationError(ValueError):
    pass


def _is_region(name: str, keywords=REGION_KEYWORDS) -> bool:
    return any(k in name for k in keywords)


def fetch_regional_generation(date_from, date_to, spec: public_api.ApiSpec | None = None,
                              fetcher: Callable = public_api.fetch_day) -> pd.Series:
    """[date_from, date_to] 전남 시간별 태양광 발전량(MWh)을 하나의 시계열로 모은다. 구간이 길면 오류."""
    start, end = pd.Timestamp(date_from).normalize(), pd.Timestamp(date_to).normalize()
    if end < start:
        raise CalibrationError("종료일이 시작일보다 빠릅니다")
    days = (end - start).days + 1
    if days > MAX_FETCH_DAYS:
        raise CalibrationError(f"조회 구간이 너무 깁니다({days}일 > {MAX_FETCH_DAYS}일) — 호출 한도 보호")
    spec = spec or public_api.load_spec("kpx_solar_regional")
    parts = []
    for d in pd.date_range(start, end, freq="D"):
        frame = fetcher(spec, d)
        if len(frame):
            parts.append(frame[frame["region"].map(_is_region)])
    if not parts:
        raise CalibrationError("조회 구간에 전남 발전량 데이터가 없습니다")
    frame = pd.concat(parts, ignore_index=True)
    series = frame.groupby("timestamp")["value"].sum(min_count=1).sort_index()
    return series.rename("jeonnam_solar_mwh")


def calibrate_sky_factor(generation: pd.Series, index, site: SolarSite, min_days: int = MIN_VALID_DAYS) -> pd.Series:
    """전남 발전량(MWh) → 시간별 날씨 계수. 관측이 없는 시간은 그날의 평균 계수, 그날 관측이 전혀 없으면 평균 날씨로 채운다."""
    idx = pd.DatetimeIndex(index)
    gen = generation.reindex(idx)
    clear = pv_model.clear_sky_ghi(idx, site)
    usable = (clear > MIN_CLEAR_GHI_WM2) & gen.notna() & (gen >= 0)
    if usable.sum() == 0:
        raise CalibrationError("보정에 쓸 수 있는 관측 시간이 없습니다")
    valid_days = int(pd.Series(usable.to_numpy(), index=idx).groupby(idx.normalize()).any().sum())
    if valid_days < min_days:
        raise CalibrationError(f"관측 일수가 부족합니다({valid_days}일 < {min_days}일) — 맑은 날 기준을 잡을 수 없습니다")
    ratio = (gen / clear).where(usable)
    scale = float(np.nanpercentile(ratio.to_numpy(), 95))
    if not scale > 0:
        raise CalibrationError("맑은 날 기준을 계산할 수 없습니다(발전량이 모두 0)")
    factor = (ratio / scale * CLEAR_REFERENCE).clip(0.0, 1.0)
    day_mean = factor.groupby(idx.normalize()).transform("mean")
    factor = factor.fillna(day_mean).fillna(pv_model.CLIMATOLOGY_SKY_FACTOR)
    return factor.rename("sky_factor")


def sky_factor_for_period(start_date, days: int, site: SolarSite, spec: public_api.ApiSpec | None = None,
                          fetcher: Callable = public_api.fetch_day, today=None) -> pd.Series:
    """에뮬레이션 기간(도착일 + 마지막 날 연장 구간)의 날씨 계수를 전남 실측으로 만든다.

    맑은 날 기준을 잡으려면 최소 MIN_VALID_DAYS일이 필요해서, 기간이 짧으면 시작일 앞쪽으로 조회 구간을 늘려
    보정에만 쓰고 결과는 에뮬레이션 기간만 잘라 돌려준다. 실측은 과거 날짜만 있으므로 오늘 이후가 섞이면 오류다.
    """
    from src.solar.demand import horizon_hours

    start = pd.Timestamp(start_date).normalize()
    idx = pd.date_range(start, periods=horizon_hours(days), freq="h")
    last_day = idx[-1].normalize()
    today = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None).normalize()
    if last_day >= today:
        raise CalibrationError("실측 발전량은 어제까지만 있습니다 — 오늘 이후가 포함된 기간에는 쓸 수 없습니다")
    fetch_start = min(start, last_day - pd.Timedelta(days=MIN_VALID_DAYS - 1))
    generation = fetch_regional_generation(fetch_start, last_day, spec, fetcher)
    wide_idx = pd.date_range(fetch_start, last_day + pd.Timedelta(hours=23), freq="h")
    factor = calibrate_sky_factor(generation, wide_idx, site)
    return factor.reindex(idx).fillna(pv_model.CLIMATOLOGY_SKY_FACTOR)
