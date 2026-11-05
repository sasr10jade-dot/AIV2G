"""EV별 AI 추천 충전 시간 추론·산출(계획서 4.1 라.) — 모바일 '충전 시간'·'예측' 탭의 근거 데이터.

한 차량의 "언제 충전하고(방전이 가능한 차량이면 언제 방전하면) 이득인가"를 계산한다. 최적화 함수
(`soc_planner.plan_single_vehicle`, 시간대별 잔량·효율·열화·방전 횟수 상한 반영)를 그대로 쓰고, 입력만 이 서비스에 맞게 만든다.
서포터즈가 입력한 출발 예정 시각·목표/최소 보장 배터리를 반영해 차량 운행에 지장이 없는 범위에서 결과를 낸다.

가격
- 충전 단가(buy): 요금표(TOU, `config/tariffs.json`).
- 방전 보상단가(sell): `config/compensation.json` 시나리오(보상 없음·SMP 연동·고정 계약). 국내에는 방전 보상 제도가 없어 가정이다.
방전은 실측이 아니라 추론이다(실증 차량·충전기에 양방향 방전 설비가 없음). 모든 금액은 가정값(verified=false)이다.

차량 정보는 항상 세션의 본인 차량으로만 구한다: 앱 가입 차량은 등록한 차종(차종 목록), 시뮬레이션 차량은 시나리오의 차종·배터리·V2G 지원 여부.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from src.grid.pricing import build_price_model, get_compensation
from src.grid.tariff import get_tariff
from src.advisory.safety_guard import verify_plan
from src.optimizer.economics import EconomicsParams, breakeven_sell_price
from src.optimizer.soc_planner import PlannerError, plan_single_vehicle
from src.registry import options
from src.timeutil import kst_now as _kst_now

DEFAULT_TARIFF_ID = "kepco_ev_example"
DEFAULT_CHARGER_KW = 7.0           # 완속충전기 출력
DEFAULT_DEPARTURE_HOUR = 8
DEFAULT_COMPENSATION_ID = "contract_example"
DEFAULT_BATTERY_KWH = 64.0
RESERVE_SOC_PCT = 20.0
SOC_BAND = (35.0, 80.0)            # 방전을 허용하는 SOC 범위(기존 최적화기와 같은 기준)
MAX_WINDOW_HOURS = 48
MIN_WINDOW_HOURS = 2
FLEET_CACHE_TTL_S = 600
FORECAST_CACHE_TTL_S = 1800


class GuideError(ValueError):
    pass


@dataclass(frozen=True)
class GuideInputs:
    current_soc_pct: float = 50.0
    need_soc_pct: float = 80.0
    departure: datetime | None = None          # 현지시각(naive). 없으면 다음 08시
    compensation_id: str = DEFAULT_COMPENSATION_ID
    min_soc_pct: float | None = None           # 서포터즈 최소 보장 배터리 — 예비 잔량(20%)보다 높으면 그 값을 하한으로 쓴다


# ---------------------------------------------------------------- 차량 정보 --

_fleet_lock = threading.Lock()
_fleet_cache: dict = {"at": 0.0, "rows": None}


def _fleet_rows(now_s: float | None = None) -> list[dict]:
    """시뮬레이션 차량 상태표 — 읽는 데 1~2초 걸려 잠시 캐시한다."""
    from src.datagen.config import SimConfig
    from src.pipeline.source import load_scenario
    from src.pipeline.store import build_vehicle_status_table

    now_s = time.monotonic() if now_s is None else now_s
    with _fleet_lock:
        if _fleet_cache["rows"] is None or now_s - _fleet_cache["at"] > FLEET_CACHE_TTL_S:
            ev_df, _c, _g, _real = load_scenario(SimConfig())
            _fleet_cache.update(at=now_s, rows=build_vehicle_status_table(ev_df))
        return _fleet_cache["rows"]


def reset_caches() -> None:
    with _fleet_lock:
        _fleet_cache.update(at=0.0, rows=None)
    with _forecast_lock:
        _forecast_cache.update(at=0.0, data=None)


def vehicle_profile(username: str, ev_id: str) -> dict:
    """본인 차량의 라벨·배터리·방전 지원 여부. 앱 가입 차량은 등록 차종, 그 외는 시뮬레이션 차량 정보를 쓴다."""
    from src.registry import service as registry

    reg = registry.active_for(username)
    if reg:
        catalog = options.load_catalog()
        model = catalog.models.get(reg["vehicle_model"])
        if model:
            return {"label": model["label"], "battery_kwh": float(model.get("battery_kwh") or DEFAULT_BATTERY_KWH),
                    "battery_assumed": model.get("battery_kwh") is None, "v2g_capable": bool(model.get("v2g_capable")),
                    "source": "registration", "demo": bool(model.get("demo"))}
    if not ev_id.startswith(registry.AUTO_EV_PREFIX):
        for row in _fleet_rows():
            if row["ev_id"] == ev_id:
                return {"label": row.get("vehicle_model") or "차량", "battery_kwh": float(row.get("battery_capacity_kwh") or DEFAULT_BATTERY_KWH),
                        "battery_assumed": row.get("battery_capacity_kwh") is None,
                        "v2g_capable": bool(row.get("v2g_capable")), "source": "fleet", "demo": False,
                        "soc_pct": row.get("soc_pct")}
    return {"label": "내 차량", "battery_kwh": DEFAULT_BATTERY_KWH, "battery_assumed": True, "v2g_capable": False,
            "source": "default", "demo": False}


# ------------------------------------------------------------------- 안내 계산 --

def kst_now() -> datetime:
    return _kst_now().to_pydatetime()


def default_departure(now: datetime, hhmm: str | None = None) -> datetime:
    """다음 출발 시각 — hhmm(서포터즈 평소 출발 시각, 'HH:MM')이 있으면 그 시각, 없으면 08시.
    지금부터 최소 안내 시간 이내라면 다음 날로 넘긴다."""
    hour, minute = (int(x) for x in hhmm.split(":")) if hhmm else (DEFAULT_DEPARTURE_HOUR, 0)
    nxt = pd.Timestamp(now).normalize() + pd.Timedelta(hours=hour, minutes=minute)
    if nxt <= pd.Timestamp(now) + pd.Timedelta(hours=MIN_WINDOW_HOURS):
        nxt += pd.Timedelta(days=1)
    return nxt.to_pydatetime()


def next_departure(hhmm: str | None, now: datetime | None = None) -> datetime | None:
    """가입 때 입력한 평소 출발 시각으로 다음 출발 시각을 만든다. 입력이 없으면 None(기본 08시를 쓴다)."""
    return default_departure(now or kst_now(), hhmm) if hhmm else None


def _runs(hours: list[int]) -> list[tuple[int, int]]:
    """정렬된 인덱스 목록을 연속 구간 (시작, 끝 포함)으로 묶는다."""
    out: list[tuple[int, int]] = []
    for h in hours:
        if out and h == out[-1][1] + 1:
            out[-1] = (out[-1][0], h)
        else:
            out.append((h, h))
    return out


def _intervals(times: list[pd.Timestamp], values: np.ndarray, price: np.ndarray, min_kwh: float = 0.05) -> list[dict]:
    active = [i for i, v in enumerate(values) if v > min_kwh]
    result = []
    for a, b in _runs(active):
        kwh = float(values[a:b + 1].sum())
        result.append({
            "start": times[a].isoformat(), "end": (times[b] + pd.Timedelta(hours=1)).isoformat(),
            "label": f"{times[a].hour:02d}~{(times[b].hour + 1) % 24:02d}시", "kwh": round(kwh, 1),
            "avg_krw_per_kwh": round(float((values[a:b + 1] * price[a:b + 1]).sum() / kwh), 1) if kwh > 0 else 0.0,
        })
    return result


def _grid_signal_price(times: list[pd.Timestamp]) -> dict:
    """'SMP 연동' 보상 시나리오용 계통 가격 — 실 SMP(KPX 키 필요)가 없어 기존 시스템과 같은 시뮬레이션 신호를 쓴다."""
    import numpy as _np
    from src.datagen.config import SimConfig
    from src.datagen.grid import generate_grid_signal

    day0 = pd.Timestamp(times[0]).normalize()
    cfg = SimConfig(start_date=day0.strftime("%Y-%m-%d"), weeks=1)
    grid = generate_grid_signal(cfg, _np.random.default_rng(cfg.random_seed))
    price = dict(zip(grid["timestamp"], grid["price_krw_per_kwh"]))
    return {t: float(price.get(t, 0.0)) for t in times}


def build_guide(profile: dict, inputs: GuideInputs, now: datetime | None = None, economics: EconomicsParams | None = None,
                tariff_id: str = DEFAULT_TARIFF_ID, participating: bool = True) -> dict:
    """participating=False(이용자가 방전 참여를 끔)이면 충전 시간만 조절한다."""
    now = now or kst_now()
    start = pd.Timestamp(now).floor("h") + pd.Timedelta(hours=1)
    dep = pd.Timestamp(inputs.departure or default_departure(now)).floor("h")
    n = int((dep - start) / pd.Timedelta(hours=1))
    if n < MIN_WINDOW_HOURS:
        raise GuideError(f"출발까지 {MIN_WINDOW_HOURS}시간 이상 남아야 안내할 수 있어요")
    if n > MAX_WINDOW_HOURS:
        raise GuideError(f"출발 시각은 앞으로 {MAX_WINDOW_HOURS}시간 이내로 정해 주세요")
    if not 0 <= inputs.current_soc_pct <= 100 or not 20 <= inputs.need_soc_pct <= 100:
        raise GuideError("배터리 값은 현재 0~100%, 필요 20~100% 범위여야 해요")
    reserve_pct = max(RESERVE_SOC_PCT, float(inputs.min_soc_pct or 0.0))
    if reserve_pct >= inputs.need_soc_pct:
        raise GuideError("최소 보장 배터리는 출발 시 필요한 배터리보다 낮아야 해요")

    economics = economics or EconomicsParams.load()
    comp = get_compensation(inputs.compensation_id)
    times = list(pd.date_range(start, periods=n, freq="h"))

    pm = build_price_model(times, _grid_signal_price(times), tariff_id, inputs.compensation_id)
    buy = np.array([pm.buy_by_time[t] for t in times])
    sell = np.array([pm.sell_by_time[t] for t in times])

    cap, soc0 = profile["battery_kwh"], float(inputs.current_soc_pct)
    need_soc = float(inputs.need_soc_pct)
    e0, target = soc0 / 100 * cap, need_soc / 100 * cap
    needed_kwh = max(target - e0, 0.0)                     # 배터리에 더 채워야 하는 에너지
    kw = DEFAULT_CHARGER_KW
    deliverable = n * kw * economics.charge_efficiency
    if needed_kwh > deliverable + 1e-9:
        raise GuideError(f"출발까지 시간이 부족해요(필요 {needed_kwh:.1f}kWh, 충전 가능 {deliverable:.1f}kWh)")
    in_band = SOC_BAND[0] <= soc0 <= SOC_BAND[1]

    # 시간대별 잔량을 상태변수로 둔 최적화(FR-O03): 잔량 (최소 보장)~100%, 방전은 35~80%에서만, 충·방전 동시 금지, 하루 방전 횟수 상한
    reserve_kwh = reserve_pct / 100 * cap
    band_kwh = (max(SOC_BAND[0], reserve_pct) / 100 * cap, SOC_BAND[1] / 100 * cap)

    def _solve(allow_discharge: bool):
        try:
            return plan_single_vehicle(
                buy, sell, e0, cap, target, economics.charge_efficiency, economics.discharge_efficiency,
                economics.degradation_krw_per_kwh, kw, kw, reserve_kwh, band_kwh, allow_discharge,
                economics.max_discharge_events_per_day, [t.date() for t in times])
        except PlannerError as exc:
            raise GuideError(str(exc)) from exc

    can_discharge = bool(profile["v2g_capable"]) and participating
    plan = _solve(can_discharge)
    # 최적화기와 별개의 코드로 이용자 조건(출발 잔량·예비 잔량·방전 범위·참여 여부)을 다시 확인한다.
    # 통과하지 못하면 방전 없는 계획으로 되돌린다(AI는 안전 조건을 대체하지 못한다).
    safety = verify_plan(plan.energy, plan.charge, plan.discharge, cap, target, reserve_kwh, band_kwh, participating)
    guard_fallback = False
    if not safety["passed"] and can_discharge:
        plan = _solve(False)
        safety = verify_plan(plan.energy, plan.charge, plan.discharge, cap, target, reserve_kwh, band_kwh, participating)
        guard_fallback = True
    charge, discharge = plan.charge, plan.discharge

    # 기준안: 도착 즉시 최대 출력으로 채움
    base = np.zeros(n)
    remaining = needed_kwh / economics.charge_efficiency
    for i in range(n):
        if remaining <= 1e-9:
            break
        base[i] = min(kw, remaining)
        remaining -= base[i]
    baseline_cost = float((base * buy).sum())
    charge_cost = float((charge * buy).sum())
    revenue = float((discharge * sell).sum())
    degradation = float(discharge.sum()) / economics.discharge_efficiency * economics.degradation_krw_per_kwh
    optimized_cost = charge_cost - revenue + degradation
    saving = baseline_cost - optimized_cost

    # SOC 궤적 — 최적화가 푼 시간대별 잔량 그대로(다시 계산하지 않는다)
    soc_path = [{"time": (start + pd.Timedelta(hours=i)).isoformat(), "soc_pct": round(float(e / cap * 100), 1)}
                for i, e in enumerate(plan.energy)]

    # 요금이 낮은 시간대(가장 싼 가격의 5% 이내) — 연속 구간 상위 3개
    low = [i for i in range(n) if buy[i] <= buy.min() * 1.05 + 1e-9]
    windows = []
    for a, b in sorted(_runs(low), key=lambda r: -(r[1] - r[0]))[:3]:
        windows.append({
            "start": times[a].isoformat(), "end": (times[b] + pd.Timedelta(hours=1)).isoformat(),
            "label": f"{times[a].hour:02d}~{(times[b].hour + 1) % 24:02d}시",
            "avg_krw_per_kwh": round(float(buy[a:b + 1].mean()), 1),
        })
    windows.sort(key=lambda w: w["start"])

    breakeven = breakeven_sell_price(float(buy.min()), economics)
    max_sell = float(sell.max()) if len(sell) else 0.0
    recommended = bool(discharge.sum() > 0.05)
    if not participating and profile["v2g_capable"]:
        reason = "방전 참여를 꺼 두셔서 충전 시간만 조절하는 방식(V1G)으로 안내해요."
    elif guard_fallback:
        reason = "안전 확인을 통과하지 못해 방전 없이 충전 시간만 조절하는 방식(V1G)으로 안내해요."
    elif not profile["v2g_capable"]:
        reason = "이 차량은 계통으로 방전(V2G)을 지원하지 않아서, 충전 시간만 조절하는 방식(V1G)을 추천해요."
    elif not in_band:
        reason = f"현재 배터리({soc0:.0f}%)가 방전을 허용하는 범위({SOC_BAND[0]:.0f}~{SOC_BAND[1]:.0f}%) 밖이라 방전은 추천하지 않아요."
    elif recommended:
        reason = "요금이 싼 시간에 충전하고 보상이 높은 시간에 내보내면 이득이에요(가정값 기준)."
    else:
        reason = (f"방전 보상단가(최고 {max_sell:.0f}원/kWh)가 손익분기(약 {breakeven:.0f}원/kWh)보다 낮아서 방전은 추천하지 않아요."
                  if max_sell <= breakeven else "이번 조건에서는 방전해도 이득이 크지 않아서 추천하지 않아요.")

    # AI Agent 기반 결과 근거 설명형 텍스트 생성 (계획서 4.1 라)
    charge_intervals = _intervals(times, charge, buy)
    chg_windows_str = ""
    if charge_intervals:
        w_strs = [f"{w['start'].split('T')[1][:5]}~{w['end'].split('T')[1][:5]}" for w in charge_intervals if 'start' in w and 'end' in w]
        chg_windows_str = ", ".join(w_strs)

    saving_val = round(saving)
    saving_pct_val = round(saving / baseline_cost * 100, 1) if baseline_cost > 1e-9 else 0.0

    exp_parts = []
    if chg_windows_str:
        exp_parts.append(f"AI 에이전트가 {chg_windows_str} 구간의 충전을 추천합니다.")
        exp_parts.append("해당 시간대는 요금이 가장 저렴한 심야 경부하 구간이며 계통 전력수요가 낮아 비용과 계통 부하를 동시에 줄입니다.")
    else:
        exp_parts.append("현재 배터리 잔량으로 다음 일정 운행이 가능하여 즉시 충전을 권장하지 않습니다.")

    if saving_val > 0:
        exp_parts.append(f"일반 즉시 충전 대비 약 {saving_val:,}원({saving_pct_val}%)의 충전비용 절감 효과가 추론됩니다(모의).")
    exp_parts.append(f"출발 예정 시각({dep.strftime('%H:%M')})까지 목표 배터리({need_soc:.0f}%)를 안전하게 충전합니다.")

    if recommended and revenue > 0:
        exp_parts.append(f"전력 피크 시간대 V2G 방전 참여를 통해 약 {round(revenue):,}원의 추가 편익이 산출되었습니다(추론 잠재량).")

    ai_agent_explanation = " ".join(exp_parts)

    return {
        "ai_agent_explanation": ai_agent_explanation,
        "vehicle": {k: profile[k] for k in ("label", "battery_kwh", "v2g_capable", "demo") if k in profile},
        "period": {"start": start.isoformat(), "departure": dep.isoformat(), "hours": n},
        "inputs": {"current_soc_pct": soc0, "need_soc_pct": need_soc, "min_soc_pct": reserve_pct,
                   "compensation_id": comp.compensation_id, "compensation_label": comp.label},
        "prices": [{"time": t.isoformat(), "buy_krw": round(float(buy[i]), 1), "sell_krw": round(float(sell[i]), 1)}
                   for i, t in enumerate(times)],
        "windows": windows,
        "plan": {"charge": _intervals(times, charge, buy), "discharge": _intervals(times, discharge, sell),
                 "charge_kwh": round(float(charge.sum()), 1), "discharge_kwh": round(float(discharge.sum()), 1),
                 "soc": soc_path, "final_soc_pct": soc_path[-1]["soc_pct"]},
        "cost": {"baseline_krw": round(baseline_cost), "optimized_krw": round(optimized_cost), "saving_krw": round(saving),
                 "saving_pct": round(saving / baseline_cost * 100, 1) if baseline_cost > 1e-9 else 0.0,
                 "discharge_revenue_krw": round(revenue), "degradation_krw": round(degradation)},
        "v2g": {"capable": bool(profile["v2g_capable"]), "recommended": recommended, "reason": reason,
                "breakeven_sell_krw_per_kwh": round(breakeven, 1), "max_sell_krw_per_kwh": round(max_sell, 1)},
        "safety": {**safety, "participating": bool(participating), "fallback": guard_fallback,
                   "min_soc_at_departure_pct": need_soc, "reserve_soc_pct": reserve_pct},
        "verified": False,
        "generation": "inferred",   # 실측이 아니라 AI 추론 결과(계획서 '수행 전제' — 실측·추론 구분 표기)
        "assumptions": {
            "note": "요금표·방전 보상단가는 가정값이고, 방전은 실제 방전이 아닌 추론 결과예요. 실제와 다를 수 있어요.",
            "tariff_id": tariff_id, "charger_kw": kw, "compensation": comp.label,
            "economics_verified": economics.verified,
        },
    }


# ----------------------------------------------------------------- 수요 예측 요약 --

_forecast_lock = threading.Lock()
_forecast_cache: dict = {"at": 0.0, "data": None}



def warm_caches_async() -> None:
    """서버 기동 직후 차량 상태표와 수요 예측 요약을 백그라운드에서 미리 데운다 — 첫 이용자 요청이 수십 초 걸리지 않게."""
    def run() -> None:
        try:
            _fleet_rows()
            demand_forecast()
        except Exception:   # noqa: BLE001
            logging.getLogger(__name__).exception("이용자 화면 캐시 예열에 실패했습니다")

    threading.Thread(target=run, daemon=True, name="owner-warm").start()


def _fetch_forecast(target: str) -> dict:
    from src.model.forecast_service import forecast_preferred
    return forecast_preferred(target, "hourly")   # ㈜아크릴 예측 우선, 없으면 기준 모델


def _extreme_windows(times: list[pd.Timestamp], values: np.ndarray, high: bool, top: int = 2) -> list[dict]:
    if len(values) == 0:
        return []
    q = np.quantile(values, 0.75 if high else 0.25)
    # 값이 같은 시간이 많으면 분위수가 최솟값·최댓값과 같아져 하루 전체가 선택된다 — 극값과 다른 시간만 후보로 둔다
    lo, hi = float(values.min()), float(values.max())
    sel = [i for i, v in enumerate(values) if (v >= q and v > lo if high else v <= q and v < hi)]
    out = []
    for a, b in sorted(_runs(sel), key=lambda r: -(r[1] - r[0]))[:top]:
        out.append({"start": times[a].isoformat(), "end": (times[b] + pd.Timedelta(hours=1)).isoformat(),
                    "label": f"{times[a].strftime('%m/%d')} {times[a].hour:02d}~{(times[b].hour + 1) % 24:02d}시",
                    "avg": round(float(values[a:b + 1].mean()), 1)})
    return sorted(out, key=lambda w: w["start"])


def demand_forecast(fetch=_fetch_forecast, now_s: float | None = None) -> dict:
    """충전 수요·V2G 가용량 48시간 예측(전체 시뮬레이션 차량 기준)과 높은·낮은 시간대. 30분 캐시."""
    now_s = time.monotonic() if now_s is None else now_s
    with _forecast_lock:
        if _forecast_cache["data"] is not None and now_s - _forecast_cache["at"] <= FORECAST_CACHE_TTL_S:
            return _forecast_cache["data"]
        charging, v2g = fetch("ev_charging_demand_kw"), fetch("ev_available_v2g_kwh")
        times = [pd.Timestamp(t) for t in charging["timestamps"]]
        c = np.array(charging["values"], dtype=float)
        v = np.array(v2g["values"], dtype=float)
        n = min(len(times), len(c), len(v))
        times, c, v = times[:n], c[:n], v[:n]
        data = {
            "series": [{"time": t.isoformat(), "charging_demand_kw": round(float(c[i]), 1),
                        "v2g_available_kwh": round(float(v[i]), 1)} for i, t in enumerate(times)],
            "charging_peaks": _extreme_windows(times, c, True), "charging_lows": _extreme_windows(times, c, False),
            "is_real_data": bool(charging.get("is_real_data") or v2g.get("is_real_data")),
            "model_versions": {"charging_demand": charging.get("model_version"), "v2g_available": v2g.get("model_version")},
            "model_source": {"charging_demand": charging.get("model_source", "baseline"), "v2g_available": v2g.get("model_source", "baseline")},
            "scope": "시뮬레이션 차량 전체 기준(내 차 한 대가 아님)",
            "note": "예측 모델은 모의(합성) 데이터로 학습돼 있어요. 실제 운행 데이터가 쌓이면 달라질 수 있어요." if not (
                charging.get("is_real_data") or v2g.get("is_real_data")) else "실증 데이터로 학습한 예측이에요.",
            "verified": False,
        }
        _forecast_cache.update(at=now_s, data=data)
        return data


# ------------------------------------------------------------------ 영어 표시 --
# 계산 결과는 그대로 두고, 화면에 보이는 고정 문구만 영어로 바꿔 돌려준다(캐시된 원본은 건드리지 않는다).

_EN_FIXED = {
    "이 차량은 계통으로 방전(V2G)을 지원하지 않아서, 충전 시간만 조절하는 방식(V1G)을 추천해요.":
        "This vehicle does not support discharging to the grid (V2G), so we only shift charging times (V1G).",
    "요금이 싼 시간에 충전하고 보상이 높은 시간에 내보내면 이득이에요(가정값 기준).":
        "Charging when prices are low and discharging when compensation is high pays off (based on assumed values).",
    "방전 참여를 꺼 두셔서 충전 시간만 조절하는 방식(V1G)으로 안내해요.":
        "Discharge participation is off, so we only shift charging times (V1G).",
    "안전 확인을 통과하지 못해 방전 없이 충전 시간만 조절하는 방식(V1G)으로 안내해요.":
        "The safety check did not pass, so we only shift charging times without discharging (V1G).",
    "이번 조건에서는 방전해도 이득이 크지 않아서 추천하지 않아요.":
        "Under these conditions discharging adds little benefit, so it is not recommended.",
    "요금표·방전 보상단가는 가정값이고, 방전은 실제 방전이 아닌 추론 결과예요. 실제와 다를 수 있어요.":
        "Tariffs and discharge compensation are assumed values, and discharging is an inferred result, not an actual discharge. Results may differ from reality.",
    "시뮬레이션 차량 전체 기준(내 차 한 대가 아님)": "Based on the whole simulated fleet (not your car alone)",
    "예측 모델은 모의(합성) 데이터로 학습돼 있어요. 실제 운행 데이터가 쌓이면 달라질 수 있어요.":
        "The forecast model is trained on simulated (synthetic) data and may change as real driving data accumulates.",
    "실증 데이터로 학습한 예측이에요.": "This forecast is trained on pilot data.",
}
_EN_PATTERNS = [
    (re.compile(r"^현재 배터리\((\d+)%\)가 방전을 허용하는 범위\((\d+)~(\d+)%\) 밖이라 방전은 추천하지 않아요\.$"),
     lambda m: f"Your battery ({m[1]}%) is outside the allowed discharge range ({m[2]}-{m[3]}%), so discharging is not recommended."),
    (re.compile(r"^방전 보상단가\(최고 (\d+)원/kWh\)가 손익분기\(약 (\d+)원/kWh\)보다 낮아서 방전은 추천하지 않아요\.$"),
     lambda m: f"The discharge compensation (max {m[1]} KRW/kWh) is below break-even (about {m[2]} KRW/kWh), so discharging is not recommended."),
]


def _en(text):
    if not isinstance(text, str):
        return text
    if text in _EN_FIXED:
        return _EN_FIXED[text]
    for pat, rep in _EN_PATTERNS:
        if pat.match(text):
            return rep(pat.match(text)) if callable(rep) else text
    return text


def localize(payload: dict, lang: str) -> dict:
    """lang == 'en'이면 안내 문구를 영어로 바꾼 사본을 돌려준다. 그 밖에는 원본 그대로."""
    if lang != "en":
        return payload
    out = dict(payload)
    if "v2g" in out:
        out["v2g"] = {**out["v2g"], "reason": _en(out["v2g"].get("reason"))}
    if "assumptions" in out:
        out["assumptions"] = {**out["assumptions"], "note": _en(out["assumptions"].get("note"))}
    for k in ("scope", "note"):
        if k in out:
            out[k] = _en(out[k])
    return out
