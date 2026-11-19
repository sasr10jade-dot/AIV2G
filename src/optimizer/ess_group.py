"""V2G 이동형 ESS 집중 실증 그룹 (신구대조표 4번 · 계획서 p.30, 요구사항 D03).

서포터즈 EV 30대 중 운행 패턴이 규칙적이고 장기 주차 대기 시간이 긴 차량 5~10대를 '이동형 ESS 집중 실증 그룹'으로
지정하고, 다음 3종 패턴을 분석해 '전기차 배터리 가상 ESS 자원화 전용 데이터셋'으로 내보낸다.

  1) 장기 주차 중 방전 허용 가능 시간대 — 연결 중이고 SOC가 하한(예비 SOC)+여유 이상이며 주행이 없는 시간
  2) 완속 충전기 체류 시간 — 완속(AC) 충전 세션의 평균 체류(시간)
  3) 심야 충전 후 주간 대기 패턴 — 심야(22~06시) 충전한 날 중 주간(09~17시)에 주행 없이 대기한 날의 비율

선정 점수(0~100) = 규칙성 30 + 장기 주차 30 + 완속 체류 20 + 심야충전·주간대기 20. 그룹 구분은 공공기관 관용차
(소속=관용 또는 업무용 차량) / 정주형 출퇴근 차량(그 외).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

GROUP_MIN, GROUP_MAX = 5, 10
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
            "score": round(score, 1),
        })
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def recommend(candidates: list[dict], n: int = GROUP_MAX) -> list[dict]:
    n = max(GROUP_MIN, min(GROUP_MAX, n))
    return candidates[:n]


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
    cols = ["timestamp", "ev_id", "ev_type", "soc_pct", "connected", "driving", "charging_kw", "battery_capacity_kwh",
            "parked_connected", "discharge_allowed", "dischargeable_kwh", "is_night", "is_daytime", "generation"]
    return ev[[c for c in cols if c in ev.columns]].reset_index(drop=True)
