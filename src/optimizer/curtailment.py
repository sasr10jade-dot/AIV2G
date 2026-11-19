"""신재생 출력제어 해소 시나리오 에뮬레이터 (신구대조표 1번 · 계획서 p.28, 요구사항 C01).

절차(계획서 문구 그대로): 공공데이터 수집 → 정제(결측·이상치 제거) → AI 패턴분석(과잉발전·피크 시간대 도출)
→ 에뮬레이터로 가상 과잉발전 상황 재현 → 충·방전 제어 적용 전후 출력제어량 비교.

마이크로그리드 = 실증 거점 3곳(config/sites.json). 거점마다 계약전력 규모의 가상 태양광을 두고, 공공데이터
재생에너지 발전량(renewable_gen_mw)을 0~1로 정규화한 패턴으로 발전량을 재현한다. 발전량이
(거점 부하 + EV 충전 + 역송 한도)를 넘는 만큼이 출력제어(버려지는 전력)다.

- 적용 전: 일반 충전(관측 충전 그대로)
- 적용 후: AI 최적 스케줄(LP) + 과잉발전 시간에 연결 중인 EV가 남는 배터리 여유만큼 추가 흡수(충전),
           흡수한 전력은 같은 날 피크 시간대에 방전해 거점 피크를 낮춘다(효율 손실 반영).

모든 가정값은 config/vpp_settlement.json(verified=false)에 있고, 결과에는 '모의(가정값)'를 표기한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src.paths import PROJECT_ROOT

CONFIG_FILE = PROJECT_ROOT / "config" / "vpp_settlement.json"
SITES_FILE = PROJECT_ROOT / "config" / "sites.json"


def load_params(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG_FILE).read_text(encoding="utf-8"))


def load_sites(path: Path | None = None) -> list[dict]:
    return json.loads(Path(path or SITES_FILE).read_text(encoding="utf-8"))["sites"]


# ------------------------------------------------------------------ 1) 정제
def clean_public_series(grid_df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """공공데이터(발전량·수요·기상) 정제 — 중복 시각 제거, 결측 보간, 음수 제거, 시간대별 이상치(IQR 3배) 대체."""
    cols = [c for c in ("power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh") if c in grid_df.columns]
    df = grid_df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    n_raw = len(df)
    df = df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    n_dup = n_raw - len(df)
    n_missing = int(df[cols].isna().sum().sum())
    df[cols] = df[cols].interpolate(limit_direction="both")
    n_negative = 0
    for c in ("power_demand_mw", "renewable_gen_mw"):
        if c in df.columns:
            neg = df[c] < 0
            n_negative += int(neg.sum())
            df.loc[neg, c] = 0.0
    n_outlier = 0
    hour = df["timestamp"].dt.hour
    for c in ("power_demand_mw", "renewable_gen_mw"):
        if c not in df.columns:
            continue
        q1 = df.groupby(hour)[c].transform(lambda s: s.quantile(0.25))
        q3 = df.groupby(hour)[c].transform(lambda s: s.quantile(0.75))
        med = df.groupby(hour)[c].transform("median")
        iqr = q3 - q1
        out = (df[c] > q3 + 3 * iqr) | (df[c] < q1 - 3 * iqr)
        out &= iqr > 0
        n_outlier += int(out.sum())
        df.loc[out, c] = med[out]
    return df, {
        "rows_in": n_raw, "rows_out": len(df), "duplicates_removed": n_dup,
        "missing_interpolated": n_missing, "negatives_zeroed": n_negative, "outliers_replaced": n_outlier,
        "rule": "중복 시각 제거 → 결측 선형보간 → 음수 0 처리 → 시간대별 IQR 3배 밖 값은 해당 시간대 중앙값으로 대체",
    }


# ------------------------------------------------------------------ 2) 에뮬레이터
def _site_load(ts: pd.Series, sites: list[dict], weekend_ratio: float, holiday: pd.Series | None) -> np.ndarray:
    profile = np.zeros(24)
    for s in sites:
        p = s.get("estimated_base_load_profile") or []
        if len(p) == 24:
            profile += np.asarray(p, dtype=float)
    load = profile[ts.dt.hour.to_numpy()]
    off = (ts.dt.dayofweek >= 5).to_numpy().copy()
    if holiday is not None:
        off = off | holiday.fillna(0).astype(bool).to_numpy()
    return np.where(off, load * weekend_ratio, load)


def emulate(
    grid_df: pd.DataFrame,
    ev_df: pd.DataFrame,
    comparison: pd.DataFrame,
    max_charge_kw: float = 7.0,
    max_discharge_kw: float = 7.0,
    charge_efficiency: float = 0.92,
    discharge_efficiency: float = 0.92,
    params: dict | None = None,
    sites: list[dict] | None = None,
    provenance: dict | None = None,
) -> dict:
    params = params or load_params()
    sites = sites if sites is not None else load_sites()
    cp = params["curtailment"]
    clean, cleaning = clean_public_series(grid_df)

    # EV 선단 시간별: 일반충전(관측), 연결 대수, 배터리 여유(kWh)
    ev = ev_df.copy()
    ev["timestamp"] = pd.to_datetime(ev["timestamp"])
    ev["headroom_kwh"] = np.where(ev["connected"], (1 - ev["soc_pct"] / 100).clip(lower=0) * ev["battery_capacity_kwh"], 0.0)
    fleet = ev.groupby("timestamp").agg(
        ev_baseline_kw=("charging_kw", "sum"),
        connected=("connected", "sum"),
        headroom_kwh=("headroom_kwh", "sum"),
    )
    comp = comparison.copy()
    comp["timestamp"] = pd.to_datetime(comp["timestamp"])
    if "discharge_kw" not in comp.columns:
        comp["discharge_kw"] = 0.0
    comp["opt_charge_kw"] = (comp["optimized_kw"] + comp["discharge_kw"]).clip(lower=0)
    df = (clean.set_index("timestamp")
          .join(fleet, how="inner")
          .join(comp.set_index("timestamp")[["opt_charge_kw", "discharge_kw"]], how="left")
          .fillna({"opt_charge_kw": 0.0, "discharge_kw": 0.0})
          .reset_index())
    if df.empty:
        raise ValueError("발전·EV 데이터의 시각이 겹치지 않아 에뮬레이션할 수 없습니다")

    total_contract_kw = float(sum(s.get("contract_power_kw", 0) for s in sites))
    pv_kwp = total_contract_kw * cp["pv_kwp_per_contract_kw"]
    ren = df["renewable_gen_mw"].astype(float)
    norm = ren / ren.max() if ren.max() > 0 else ren * 0
    df["pv_kw"] = norm * pv_kwp * cp["over_generation_factor"]
    df["site_load_kw"] = _site_load(df["timestamp"], sites, cp["weekend_load_ratio"], df.get("is_holiday"))
    export_limit_kw = cp["export_limit_ratio"] * total_contract_kw

    # 적용 전: 일반 충전
    df["curtail_before_kw"] = (df["pv_kw"] - df["site_load_kw"] - df["ev_baseline_kw"] - export_limit_kw).clip(lower=0)
    # 적용 후: AI 최적 스케줄(충전·방전 반영) + 과잉발전 시간 추가 흡수
    net_after = df["site_load_kw"] + df["opt_charge_kw"] - df["discharge_kw"]
    surplus_after_opt = (df["pv_kw"] - net_after - export_limit_kw).clip(lower=0)
    cap_kw = (df["connected"] * max_charge_kw - df["opt_charge_kw"]).clip(lower=0)
    df["date"] = df["timestamp"].dt.normalize()
    # 배터리 여유는 하루 단위로 누적 차감한다 — 과잉발전 첫 시간의 선단 여유(kWh)를 그날 흡수 상한으로 쓴다
    absorb = np.zeros(len(df))
    surplus_np, cap_np = surplus_after_opt.to_numpy(), cap_kw.to_numpy()
    room_np = (df["headroom_kwh"] - df["opt_charge_kw"]).clip(lower=0).to_numpy()
    for _d, idx in df.groupby("date").indices.items():
        remaining = None
        for i in idx:
            if surplus_np[i] <= 0:
                continue
            if remaining is None:
                remaining = room_np[i]
            a = min(surplus_np[i], cap_np[i], remaining)
            absorb[i] = a
            remaining -= a
    df["absorb_kw"] = absorb
    df["curtail_after_kw"] = surplus_after_opt - df["absorb_kw"]

    # 흡수 전력을 같은 날 피크 시간대(태양광이 빠진 저녁 순부하 피크)에 방전 — 효율 손실 반영, 연결 대수 x 방전 출력 한도
    df["is_peak"] = df["timestamp"].dt.hour.isin(cp["peak_hours"])
    df["peak_discharge_kw"] = 0.0
    eff = charge_efficiency * discharge_efficiency
    for _d, g in df.groupby("date"):
        energy = float(g["absorb_kw"].sum()) * eff
        peak_idx = g.index[g["is_peak"]]
        if energy <= 0 or len(peak_idx) == 0:
            continue
        cap = (df.loc[peak_idx, "connected"] * max_discharge_kw).to_numpy(dtype=float)
        if cap.sum() <= 0:
            continue
        alloc = np.minimum(cap, cap / cap.sum() * energy)
        df.loc[peak_idx, "peak_discharge_kw"] = alloc

    # 계통에서 받는 전력(수전) = max(0, 부하 - 태양광) — 기본요금·피크는 이 값 기준
    load_before = (df["site_load_kw"] + df["ev_baseline_kw"] - df["pv_kw"]).clip(lower=0)
    load_after = (net_after + df["absorb_kw"] - df["peak_discharge_kw"] - df["pv_kw"]).clip(lower=0)
    pv_kwh = float(df["pv_kw"].sum())
    cb, ca = float(df["curtail_before_kw"].sum()), float(df["curtail_after_kw"].sum())
    absorbed = float(df["absorb_kw"].sum())

    # 3) 패턴분석 — 시간대별 과잉발전 빈도·평균 초과량, 피크 시간대
    n_days = max(df["date"].nunique(), 1)
    by_hour = df.groupby(df["timestamp"].dt.hour).agg(
        surplus_days=("curtail_before_kw", lambda s: int((s > 0).sum())),
        curtail_before_kw=("curtail_before_kw", "mean"),
        curtail_after_kw=("curtail_after_kw", "mean"),
        pv_kw=("pv_kw", "mean"),
        site_load_kw=("site_load_kw", "mean"),
    ).reset_index().rename(columns={"timestamp": "hour"})
    by_hour["surplus_freq_pct"] = (by_hour["surplus_days"] / n_days * 100).round(1)
    surplus_hours = by_hour.loc[by_hour["surplus_freq_pct"] >= 20, "hour"].astype(int).tolist()
    # 피크 시간대 도출: 순부하(거점 부하 + 일반충전 - 태양광) 시간대 평균 상위
    load_profile = load_before.groupby(df["timestamp"].dt.hour).mean()
    derived_peak = sorted(load_profile.sort_values(ascending=False).head(len(cp["peak_hours"])).index.astype(int).tolist())
    by_month = df.groupby(df["timestamp"].dt.to_period("M")).agg(
        curtail_before_kwh=("curtail_before_kw", "sum"), curtail_after_kwh=("curtail_after_kw", "sum"),
        absorbed_kwh=("absorb_kw", "sum"), pv_kwh=("pv_kw", "sum"),
    ).reset_index()
    by_month["month"] = by_month["timestamp"].astype(str)

    daily = df.groupby("date").agg(
        curtail_before_kwh=("curtail_before_kw", "sum"), curtail_after_kwh=("curtail_after_kw", "sum"),
        absorbed_kwh=("absorb_kw", "sum"), peak_discharge_kwh=("peak_discharge_kw", "sum"),
    ).reset_index().tail(56)

    value = params["benefits"].get("curtailment_value_krw_per_kwh")
    price = df["price_krw_per_kwh"] if value is None else pd.Series(float(value), index=df.index)
    benefit_krw = float((df["absorb_kw"] * price).sum())

    is_measured = (provenance or {}).get("grid_signal") == "public"
    return {
        "label": "실측 공공데이터 패턴 기반 모의" if is_measured else "모의(합성 발전 패턴·가정값)",
        "verified": bool(params.get("verified")),
        "pipeline": ["공공데이터 수집", "정제(결측·이상치)", "AI 패턴분석(과잉발전·피크 시간대)", "에뮬레이터 재현", "전후 출력제어량 비교"],
        "cleaning": cleaning,
        "scenario": {
            "pv_kwp": round(pv_kwp, 1), "over_generation_factor": cp["over_generation_factor"],
            "export_limit_kw": round(export_limit_kw, 1), "total_contract_kw": total_contract_kw,
            "peak_hours": cp["peak_hours"], "n_days": n_days, "n_sites": len(sites),
        },
        "patterns": {
            "surplus_hours": surplus_hours,
            "derived_peak_hours": derived_peak,
            "by_hour": [{k: (round(float(v), 2) if isinstance(v, (float, np.floating)) else int(v)) for k, v in r.items()}
                        for r in by_hour.to_dict("records")],
            "by_month": [{"month": r["month"], **{k: round(float(r[k]), 1) for k in ("curtail_before_kwh", "curtail_after_kwh", "absorbed_kwh", "pv_kwh")}}
                         for r in by_month.to_dict("records")],
        },
        "totals": {
            "pv_kwh": round(pv_kwh, 1),
            "curtail_before_kwh": round(cb, 1),
            "curtail_after_kwh": round(ca, 1),
            "curtail_reduction_kwh": round(cb - ca, 1),
            "curtail_reduction_pct": round((cb - ca) / cb * 100, 1) if cb > 0 else 0.0,
            "absorbed_kwh": round(absorbed, 1),
            "peak_discharge_kwh": round(float(df["peak_discharge_kw"].sum()), 1),
            "self_consumption_before_pct": round((pv_kwh - cb) / pv_kwh * 100, 1) if pv_kwh > 0 else None,
            "self_consumption_after_pct": round((pv_kwh - ca) / pv_kwh * 100, 1) if pv_kwh > 0 else None,
            "site_peak_before_kw": round(float(load_before.max()), 1),
            "site_peak_after_kw": round(float(load_after.max()), 1),
            "peak_window_avg_before_kw": round(float(load_before[df["is_peak"]].mean()), 1) if df["is_peak"].any() else None,
            "peak_window_avg_after_kw": round(float(load_after[df["is_peak"]].mean()), 1) if df["is_peak"].any() else None,
            "curtailment_benefit_krw": round(benefit_krw, 0),
        },
        "daily": {
            "dates": [d.strftime("%Y-%m-%d") for d in daily["date"]],
            "curtail_before_kwh": [round(float(v), 1) for v in daily["curtail_before_kwh"]],
            "curtail_after_kwh": [round(float(v), 1) for v in daily["curtail_after_kwh"]],
            "absorbed_kwh": [round(float(v), 1) for v in daily["absorbed_kwh"]],
            "peak_discharge_kwh": [round(float(v), 1) for v in daily["peak_discharge_kwh"]],
        },
        # 정산(E03)·재원별 편익(S01)이 쓰는 시간별 원자료(화면 응답에서는 뺀다)
        "_hourly": df[["timestamp", "absorb_kw", "peak_discharge_kw", "price_krw_per_kwh", "is_peak",
                       "site_load_kw", "ev_baseline_kw", "opt_charge_kw", "discharge_kw"]],
        "_load_before": load_before, "_load_after": load_after,
    }


def public_view(result: dict) -> dict:
    """API 응답용 — 내부 원자료(_로 시작하는 키)를 뺀다."""
    return {k: v for k, v in result.items() if not k.startswith("_")}
