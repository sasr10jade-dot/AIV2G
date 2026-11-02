"""태양광 연계 충전소 에뮬레이터 — 나주 일사·태양광 모델 + 가정한 충전 수요로 하루~수 주를 모의 운영한다.

같은 수요·같은 날씨에서 두 방식을 비교한다.
- baseline: 도착 즉시 충전(현행 운영)
- optimized: 태양광이 있는 시간·요금이 싼 시간으로 충전을 옮기는 최적 스케줄

`plan_with`로 최적화가 쓰는 발전량 정보를 정한다(평가는 항상 실제 날씨로 한다).
- "perfect": 실제 발전량을 안다고 가정(이론상 상한)
- "climatology": 평균 날씨만 안다고 가정(예보가 없을 때의 현실적 계획) — 예측 오차로 인한 손실을 볼 수 있다

모든 입력은 사이트 설정의 가정값이므로 결과는 `verified=false`(가정값·모의)다. 실제 일사·발전 관측이 생기면
`pv_model.sky_factor_from_ghi`로 날씨 계수를 교체한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.grid.tariff import get_tariff
from src.solar import dispatch, pv_model
from src.solar.demand import generate_sessions, horizon_hours
from src.solar.site import SolarSite

MAX_DAYS = 31
PLAN_MODES = ("perfect", "climatology")


class EmulationError(ValueError):
    pass


def _label_from_factor(mean_factor: float) -> str:
    return "clear" if mean_factor >= 0.85 else "partly" if mean_factor >= 0.55 else "cloudy" if mean_factor >= 0.3 else "rain"


def run_emulation(site: SolarSite, start_date, days: int = 7, seed: int = 0, plan_with: str = "perfect",
                  sky_factor: pd.Series | None = None, weather_source: str = "model") -> dict:
    """sky_factor를 주면(예: 전남 실측으로 보정한 날씨 계수) 모의 날씨 대신 쓴다. 길이·시각은 에뮬레이션 구간과 같아야 한다."""
    if not 1 <= days <= MAX_DAYS:
        raise EmulationError(f"days는 1~{MAX_DAYS}이어야 합니다")
    if plan_with not in PLAN_MODES:
        raise EmulationError(f"plan_with는 {PLAN_MODES} 중 하나여야 합니다")

    start = pd.Timestamp(start_date).normalize()
    hours = horizon_hours(days)
    idx = pd.date_range(start, periods=hours, freq="h")

    if sky_factor is not None:
        sky = sky_factor.reindex(idx)
        if sky.isna().any():
            raise EmulationError("주입한 날씨 계수가 에뮬레이션 구간을 모두 덮지 못합니다")
    else:
        sky = pv_model.sky_factor_series(idx, seed)
    pv_actual = pv_model.pv_power_kw(idx, site, sky_factor=sky).to_numpy()
    pv_plan = pv_actual if plan_with == "perfect" else pv_model.pv_power_kw(
        idx, site, sky_factor=pv_model.CLIMATOLOGY_SKY_FACTOR).to_numpy()

    tariff = get_tariff(site.tariff_id)
    price = tariff.price_series(idx).to_numpy()

    sessions, unserved, unmet = generate_sessions(site, start, days, seed)
    base_sched = dispatch.baseline_schedule(sessions, site.charger_kw, hours)
    opt_sched, status = dispatch.optimize_schedule(sessions, site.charger_kw, pv_plan, price)

    args = (pv_actual, price, site.pv_price_krw_per_kwh, site.grid_import_limit_kw)
    base = dispatch.evaluate(sessions, base_sched, *args)
    opt = dispatch.evaluate(sessions, opt_sched, *args)

    b, o = base["summary"], opt["summary"]
    comparison = {
        "self_consumption_gain_pct_pts": round(o["self_consumption_pct"] - b["self_consumption_pct"], 1),
        "solar_share_gain_pct_pts": round(o["solar_share_pct"] - b["solar_share_pct"], 1),
        "grid_import_reduction_kwh": round(b["grid_import_kwh"] - o["grid_import_kwh"], 1),
        "grid_cost_saving_krw": round(b["grid_cost_krw"] - o["grid_cost_krw"], 0),
        "driver_cost_saving_krw": round(b["driver_cost_krw"] - o["driver_cost_krw"], 0),
        "driver_cost_saving_pct": round((b["driver_cost_krw"] - o["driver_cost_krw"]) / b["driver_cost_krw"] * 100, 1)
        if b["driver_cost_krw"] > 1e-9 else 0.0,
        "grid_peak_reduction_kw": round(b["grid_peak_kw"] - o["grid_peak_kw"], 1),
    }

    hourly = pd.DataFrame({
        "timestamp": [t.isoformat() for t in idx],
        "pv_kw": np.round(pv_actual, 2),
        "price_krw_per_kwh": price,
        "baseline_load_kw": np.round(base["hourly"]["load_kw"], 2),
        "optimized_load_kw": np.round(opt["hourly"]["load_kw"], 2),
        "baseline_grid_kw": np.round(base["hourly"]["grid_kw"], 2),
        "optimized_grid_kw": np.round(opt["hourly"]["grid_kw"], 2),
    })
    days_idx = idx.normalize()
    daily = []
    for day in sorted(set(days_idx))[:days]:
        mask = np.asarray(days_idx == day)
        pv_day = float(pv_actual[mask].sum())
        daily.append({
            "date": day.date().isoformat(),
            "sky": pv_model.sky_state_for_day(day, seed)[0] if sky_factor is None
            else _label_from_factor(float(sky[np.asarray(days_idx == day) & (np.asarray(idx.hour) >= 9) & (np.asarray(idx.hour) <= 15)].mean())),
            "pv_kwh": round(pv_day, 1),
            "baseline_pv_used_kwh": round(float(base["hourly"]["pv_used_kw"][mask].sum()), 1),
            "optimized_pv_used_kwh": round(float(opt["hourly"]["pv_used_kw"][mask].sum()), 1),
        })

    return {
        "site": site.public_dict(),
        "period": {"start": start.date().isoformat(), "days": days, "horizon_hours": hours},
        "seed": seed, "plan_with": plan_with, "weather_source": weather_source, "optimizer_status": status,
        "demand": {"sessions_served": len(sessions), "sessions_unserved": unserved,
                   "unmet_kwh": round(unmet, 1), "requested_kwh": round(sum(s.requested_kwh for s in sessions), 1)},
        "baseline": b, "optimized": o, "comparison": comparison,
        "hourly": hourly.to_dict(orient="records"), "daily": daily,
        "per_session": {"baseline": base["per_session"], "optimized": opt["per_session"]},
        "verified": bool(site.verified and tariff.verified),
        "assumptions": {
            "note": "사이트 설비·충전기·수요 패턴·날씨 확률·단가는 모두 가정값이며 verified=false다. 결과는 '가정값(모의)'으로 표기한다.",
            "tariff_id": tariff.tariff_id, "tariff_verified": tariff.verified, "site_verified": site.verified,
            "horizon_note": "마지막 날 늦게 도착한 세션이 끝날 때까지 시뮬레이션을 연장했다(모든 지표는 연장 구간 포함).",
        },
    }
