"""V2G 이동형 ESS 집중 실증 그룹 (신구대조표 4번 · 계획서 p.30, 요구사항 D03).

서포터즈 EV 30대 중 운행 패턴이 규칙적이고 장기 주차 대기 시간이 긴 차량 5~10대를 '이동형 ESS 집중 실증 그룹'으로
지정하고, 다음 3종 패턴을 분석해 '전기차 배터리 가상 ESS 자원화 전용 데이터셋'으로 내보낸다.

  1) 장기 주차 중 방전 허용 가능 시간대 — 연결 중이고 SOC가 하한(예비 SOC)+여유 이상이며 주행이 없는 시간
  2) 완속 충전기 체류 시간 — 완속(AC) 충전 세션의 평균 체류(시간)
  3) 심야 충전 후 주간 대기 패턴 — 심야(22~06시) 충전한 날 중 주간(09~17시)에 주행 없이 대기한 날의 비율

선정 점수(0~100) = 규칙성 30 + 장기 주차 30 + 완속 체류 20 + 심야충전·주간대기 20. 그룹 구분은 공공기관 관용차
(소속=관용 또는 업무용 차량) / 정주형 출퇴근 차량(그 외).

평가의견 13 반영 문안: '거점 주차 8시간 이상·주 3회 이상 연결' 차량 8대를 지정 기준으로 하고(meets_criteria 우선,
그다음 점수 순), 연결~해제 체류 시간·방전 허용 시간대·플러그인 비중을 추가로 산출하며, 그룹 합산 가상 ESS
용량(kWh)·출력(kW)·시간대별 동시 가용 대수를 낸다(aggregate_capacity). 5분 단위 SOC는 OBD 실측 연계 시 그 해상도로
들어오고, 합성 데이터는 시간 단위다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

GROUP_MIN, GROUP_MAX = 5, 10
GROUP_DEFAULT = 8                 # 평가의견 13: 8대 지정
CRITERIA_PARK_HOURS = 8           # 거점 주차(연결 유지) 8시간 이상
CRITERIA_PER_WEEK = 3             # 주 3회 이상
MAX_DISCHARGE_KW = 7.0            # 완속 양방향 충전기 1대 방전 출력(가정)
NIGHT_HOURS = set(range(22, 24)) | set(range(0, 6))
DAY_HOURS = set(range(9, 18))
DISCHARGE_SOC_FLOOR_PCT = 40.0   # 예비 SOC 20% + 귀가·출발 여유 20%p


def _regularity(g: pd.DataFrame) -> float:
    """요일·시간대별 연결 여부가 주마다 얼마나 같은지(0~1) — 같은 (요일,시간) 칸의 연결 비율이 0이나 1에 가까울수록 규칙적."""
    p = g.groupby([g["timestamp"].dt.dayofweek, g["timestamp"].dt.hour])["connected"].mean()
    return float((np.abs(p - 0.5) * 2).mean()) if len(p) else 0.0


def _long_parking_hours(g: pd.DataFrame) -> float:
    """연속 연결(주행 없음) 구간 중 4시간 이상인 구간의 하루 평균 시간."""
    s = (g["connected"] & ~g["driving"].fillna(False).astype(bool)).astype(int).to_numpy()
    total, run = 0, 0
    for v in s:
        if v:
            run += 1
        else:
            if run >= 4:
                total += run
            run = 0
    if run >= 4:
        total += run
    days = max(g["timestamp"].dt.normalize().nunique(), 1)
    return total / days


def _connected_runs(g: pd.DataFrame) -> list[int]:
    """연결(주행 없음) 연속 구간 길이(관측 칸 수, 시간 단위 데이터면 시간) 목록 — 연결~해제 체류 시간."""
    s = (g["connected"].astype(bool) & ~g["driving"].fillna(False).astype(bool)).astype(int).to_numpy()
    runs, run = [], 0
    for v in s:
        if v:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return runs


def _plugin_ratio(g: pd.DataFrame) -> float:
    """플러그인 비중 — 주차(주행 아님) 시간 중 충전기에 연결된 시간의 비율."""
    parked = ~g["driving"].fillna(False).astype(bool)
    return float(g.loc[parked, "connected"].astype(bool).mean()) if parked.any() else 0.0


def _night_then_day_idle(g: pd.DataFrame) -> float:
    g = g.assign(date=g["timestamp"].dt.normalize(), hour=g["timestamp"].dt.hour)
    nights = g[g["hour"].isin(NIGHT_HOURS) & (g["charging_kw"] > 0)]["date"].unique()
    if len(nights) == 0:
        return 0.0
    day = g[g["hour"].isin(DAY_HOURS)]
    idle = day.groupby("date")["driving"].apply(lambda s: not s.fillna(False).astype(bool).any())
    return float(idle.reindex(nights).fillna(False).mean())


def _discharge_window(g: pd.DataFrame, soc_floor: float) -> dict:
    ok = g["connected"] & ~g["driving"].fillna(False).astype(bool) & (g["soc_pct"] >= soc_floor)
    by_hour = ok.groupby(g["timestamp"].dt.hour).mean()
    hours = [int(h) for h, v in by_hour.items() if v >= 0.5]
    days = max(g["timestamp"].dt.normalize().nunique(), 1)
    return {"typical_hours": hours, "allowed_hours_per_day": round(float(ok.sum()) / days, 1)}


def analyze_candidates(ev_df: pd.DataFrame, charger_df: pd.DataFrame | None = None, affiliation_of=None,
                       soc_floor_pct: float = DISCHARGE_SOC_FLOOR_PCT) -> list[dict]:
    """차량별 3종 패턴 지표와 선정 점수를 계산해 점수 내림차순으로 돌려준다."""
    ev = ev_df.copy()
    ev["timestamp"] = pd.to_datetime(ev["timestamp"])
    ev = ev.sort_values(["ev_id", "timestamp"])
    slow_dwell = {}
    if charger_df is not None and not charger_df.empty and "ev_id" in charger_df.columns:
        slow = charger_df[charger_df.get("charger_type", pd.Series("", index=charger_df.index)).astype(str).str.startswith("AC")]
        slow_dwell = slow.groupby("ev_id")["duration_h"].mean().to_dict()

    rows = []
    for ev_id, g in ev.groupby("ev_id"):
        reg = _regularity(g)
        park = _long_parking_hours(g)
        dwell = float(slow_dwell.get(ev_id, 0.0) or 0.0)
        ndi = _night_then_day_idle(g)
        win = _discharge_window(g, soc_floor_pct)
        score = 30 * reg + 30 * min(park / 16.0, 1.0) + 20 * min(dwell / 8.0, 1.0) + 20 * ndi
        runs = _connected_runs(g)
        weeks = max((g["timestamp"].max() - g["timestamp"].min()).days / 7.0, 1.0)
        long_per_week = sum(1 for r in runs if r >= CRITERIA_PARK_HOURS) / weeks
        ev_type = str(g["ev_type"].iloc[0]) if "ev_type" in g.columns else ""
        aff, aff_src = affiliation_of(ev_id) if affiliation_of else ("private", "미지정")
        group_type = "public_fleet" if aff == "public" or "업무" in ev_type else "resident_commuter"
        rows.append({
            "ev_id": ev_id, "ev_type": ev_type, "affiliation": aff, "affiliation_source": aff_src,
            "group_type": group_type,
            "group_label": "공공기관 관용차" if group_type == "public_fleet" else "정주형 출퇴근 차량",
            "regularity": round(reg, 3),
            "long_parking_h_per_day": round(park, 1),
            "slow_charger_dwell_h": round(dwell, 2),
            "night_charge_day_idle_ratio": round(ndi, 3),
            "discharge_allowed_hours": win["typical_hours"],
            "discharge_allowed_h_per_day": win["allowed_hours_per_day"],
            "avg_dwell_h": round(float(np.mean(runs)), 1) if runs else 0.0,
            "connections_per_week": round(len(runs) / weeks, 1),
            "long_parking_per_week": round(long_per_week, 1),
            "plugin_ratio": round(_plugin_ratio(g), 3),
            "meets_criteria": long_per_week >= CRITERIA_PER_WEEK,
            "score": round(score, 1),
        })
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def recommend(candidates: list[dict], n: int = GROUP_DEFAULT) -> list[dict]:
    """기준(주차 8시간 이상 주 3회 이상) 충족 차량을 먼저, 그다음 점수 순으로 n대(기본 8대)."""
    n = max(GROUP_MIN, min(GROUP_MAX, n))
    return sorted(candidates, key=lambda c: (not c.get("meets_criteria", False), -c["score"]))[:n]


def aggregate_capacity(ev_df: pd.DataFrame, members: list[str], soc_floor_pct: float = DISCHARGE_SOC_FLOOR_PCT,
                       max_discharge_kw: float = MAX_DISCHARGE_KW) -> dict:
    """그룹 합산 가상 ESS — 시간대별 동시 가용 대수(방전 허용 상태 차량 수 평균), 가용 용량 kWh, 가용 출력 kW."""
    if not members:
        return {"n_members": 0, "by_hour": [], "summary": None}
    ds = build_dataset(ev_df, members, soc_floor_pct)
    if ds.empty:
        return {"n_members": len(members), "by_hour": [], "summary": None}
    per_ts = ds.groupby("timestamp").agg(available=("discharge_allowed", "sum"), kwh=("dischargeable_kwh", "sum"))
    per_ts["kw"] = per_ts["available"] * max_discharge_kw
    hourly = per_ts.groupby(per_ts.index.hour).agg(available=("available", "mean"), kwh=("kwh", "mean"), kw=("kw", "mean"))
    by_hour = [{"hour": int(h), "concurrent_available": round(float(r["available"]), 1),
                "capacity_kwh": round(float(r["kwh"]), 1), "power_kw": round(float(r["kw"]), 1)} for h, r in hourly.iterrows()]
    best = max(by_hour, key=lambda r: r["capacity_kwh"])
    return {
        "n_members": len(members),
        "by_hour": by_hour,
        "summary": {
            "avg_capacity_kwh": round(float(per_ts["kwh"].mean()), 1),
            "avg_power_kw": round(float(per_ts["kw"].mean()), 1),
            "max_concurrent": int(per_ts["available"].max()),
            "best_hour": best["hour"], "best_hour_capacity_kwh": best["capacity_kwh"], "best_hour_power_kw": best["power_kw"],
        },
        "assumptions": f"방전 하한 SOC {soc_floor_pct:.0f}%, 차량 1대 방전 출력 {max_discharge_kw:.0f}kW(가정) — 추론값",
    }


def build_dataset(ev_df: pd.DataFrame, members: list[str], soc_floor_pct: float = DISCHARGE_SOC_FLOOR_PCT) -> pd.DataFrame:
    """전기차 배터리 가상 ESS 자원화 전용 데이터셋 — 그룹 차량의 시간별 행에 ESS 관점 파생 컬럼을 붙인다."""
    ev = ev_df[ev_df["ev_id"].isin(members)].copy()
    ev["timestamp"] = pd.to_datetime(ev["timestamp"])
    ev = ev.sort_values(["ev_id", "timestamp"])
    driving = ev["driving"].fillna(False).astype(bool)
    ev["parked_connected"] = ev["connected"] & ~driving
    ev["discharge_allowed"] = ev["parked_connected"] & (ev["soc_pct"] >= soc_floor_pct)
    ev["dischargeable_kwh"] = np.where(ev["discharge_allowed"],
                                       (ev["soc_pct"] - soc_floor_pct).clip(lower=0) / 100 * ev["battery_capacity_kwh"], 0.0)
    ev["hour"] = ev["timestamp"].dt.hour
    ev["is_night"] = ev["hour"].isin(NIGHT_HOURS)
    ev["is_daytime"] = ev["hour"].isin(DAY_HOURS)
    ev["generation"] = "inferred"   # 방전 가능량은 SOC·연결 상태로 추론한 값
    # 연결~해제 체류: 연속 연결(주행 없음) 구간 번호와 그 구간 길이(시간) — 평가의견 13 추가 수집 항목
    newrun = ev["parked_connected"] & ~ev.groupby("ev_id")["parked_connected"].shift(1, fill_value=False)
    ev["plug_session"] = newrun.groupby(ev["ev_id"]).cumsum().where(ev["parked_connected"])
    ev["plug_session_h"] = ev.groupby(["ev_id", "plug_session"])["parked_connected"].transform("size").where(ev["parked_connected"])
    ev["soc_resolution"] = "1h"   # OBD 실측 연계 시 5분 단위 SOC로 대체(평가의견 13)
    cols = ["timestamp", "ev_id", "ev_type", "soc_pct", "connected", "driving", "charging_kw", "battery_capacity_kwh",
            "parked_connected", "plug_session", "plug_session_h", "discharge_allowed", "dischargeable_kwh", "is_night", "is_daytime",
            "soc_resolution", "generation"]
    return ev[[c for c in cols if c in ev.columns]].reset_index(drop=True)
