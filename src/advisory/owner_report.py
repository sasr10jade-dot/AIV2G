"""서포터즈 '내 충전 리포트' — 최근 4주 충전·주행 지표, 다른 실증 차량과의 비교, 조합 인사이트(AIV2GAPP F.O 리포트 이식).

계획서 4.1 라 "EV별 AI 추천 충전 시간 추론 및 산출, 결과 근거를 설명 형태로 제공"의 근거 화면이다. LLM을 쓰지 않고
정해진 산식과 문장 틀로만 설명한다.

- 충전 지표는 차량 시간별 기록(ev_telemetry)에서 계산한다: 충전 중인 시간이 이어진 구간 = 충전 1회, 그 시간의 kW 합 = 충전량.
- 주행 지표는 서포터즈가 입력한 누적 주행거리(odometer_logs)가 2번 이상 있을 때만 계산한다(차량 기록에 주행거리가 없다).
- 요금은 계통 신호의 시간별 단가(price_krw_per_kwh, 모의·가정값일 수 있음), 피크 시간대는 효용성 검증과 같은 정의
  (날짜별 전력수요 상위 25%)를 쓴다.
- 비교군은 같은 기간의 다른 실증 차량 전체의 중앙값이다(비교군을 가상으로 만들지 않는다).
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from src.optimizer.effect_metrics import peak_hours

WINDOW_DAYS = 28
MIN_SOC_FLOOR_PCT = 30.0      # 최소 보장 배터리 제안의 하한(AIV2GAPP B.O 기본값)
MIN_SOC_MARGIN_PCT = 10.0     # 하루 운행분 × 1.5에 더하는 여유
DAILY_USE_FACTOR = 1.5


def _r1(x):
    return None if x is None or pd.isna(x) else round(float(x), 1)


def _episodes(g: pd.DataFrame) -> pd.DataFrame:
    """충전 중인 시간이 이어진 구간 = 충전 1회. 반환: (start, kwh, peak_kwh, cost_krw)."""
    g = g.sort_values("timestamp")
    on = g["charging_kw"] > 0.05
    if not on.any():
        return pd.DataFrame(columns=["start", "kwh", "peak_kwh", "cost_krw"])
    gap = g["timestamp"].diff().ne(pd.Timedelta(hours=1)) | ~on.shift(fill_value=False)
    run = (gap & on).cumsum()
    c = g[on].assign(_run=run[on])
    return c.groupby("_run").agg(start=("timestamp", "min"), kwh=("charging_kw", "sum"),
                                 peak_kwh=("_peak_kwh", "sum"), cost_krw=("_cost", "sum")).reset_index(drop=True)


def _metrics(g: pd.DataFrame, weeks: float) -> dict:
    ep = _episodes(g)
    n, kwh = len(ep), float(ep["kwh"].sum()) if len(ep) else 0.0
    return {
        "sessions": n,
        "per_week": _r1(n / weeks),
        "avg_kwh": _r1(kwh / n) if n else None,
        "kwh_per_week": _r1(kwh / weeks),
        "peak_share_pct": _r1(ep["peak_kwh"].sum() / kwh * 100) if kwh > 0 else None,
        "won_per_kwh": round(float(ep["cost_krw"].sum()) / kwh) if kwh > 0 else None,
        "_kwh": kwh, "_cost": float(ep["cost_krw"].sum()) if n else 0.0,
    }


def _prepare(ev_df: pd.DataFrame, grid_df: pd.DataFrame) -> pd.DataFrame:
    df = ev_df[["timestamp", "ev_id", "charging_kw", "soc_pct"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    peaks = peak_hours(grid_df) if grid_df is not None and not grid_df.empty else set()
    price = dict(zip(pd.to_datetime(grid_df["timestamp"]), grid_df["price_krw_per_kwh"])) if grid_df is not None and "price_krw_per_kwh" in grid_df.columns else {}
    hour = df["timestamp"].dt.floor("h")
    df["_peak_kwh"] = df["charging_kw"].where(hour.isin(peaks), 0.0)
    df["_cost"] = df["charging_kw"] * hour.map(price).fillna(0.0)
    return df


def _driving(odometer: list[dict], energy_between, since) -> dict:
    """누적 주행거리 입력값으로 주간 주행·전비·1km당 요금. 창 안에 입력이 2번 이상 있어야 계산한다."""
    pts = [(pd.Timestamp(o["recorded_at"]).tz_localize(None) if pd.Timestamp(o["recorded_at"]).tzinfo else pd.Timestamp(o["recorded_at"]), o["odometer_km"])
           for o in odometer]
    pts = [p for p in pts if p[0] >= pd.Timestamp(since)]
    out = {"entries": len(pts), "km_per_week": None, "km_per_kwh": None, "won_per_km": None}
    if len(pts) < 2:
        return out
    (t0, k0), (t1, k1) = pts[0], pts[-1]
    km = k1 - k0
    days = max(1.0, (t1 - t0).total_seconds() / 86400)
    kwh, cost = energy_between(t0, t1)
    out["km_per_week"] = round(km / days * 7)
    out["km_per_kwh"] = _r1(km / kwh) if kwh > 0 else None
    out["won_per_km"] = _r1(cost / km) if km > 0 else None
    return out


def _compare(mine, peers: list, higher_is: str) -> dict | None:
    vals = sorted(v for v in peers if v is not None)
    if mine is None or not vals:
        return None
    med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    rank = sum(v < mine for v in vals) / len(vals) * 100
    return {"median": _r1(med), "percentile": round(rank), "higher_is": higher_is}


def build_report(ev_id: str, ev_df: pd.DataFrame, grid_df: pd.DataFrame, battery_kwh: float,
                 odometer: list[dict] | None = None, min_soc_pct: float | None = None,
                 target_soc_pct: float | None = None) -> dict:
    """ev_id 차량의 최근 4주 리포트. 그 차량 기록이 없으면 has_data=False."""
    if ev_df is None or ev_df.empty or ev_id not in set(ev_df["ev_id"]):
        return {"has_data": False, "ev_id": ev_id,
                "message": "아직 내 차량의 충전 기록이 없어요. OBD가 연결되고 기록이 쌓이면 리포트가 만들어져요."}
    df = _prepare(ev_df, grid_df)
    end = df["timestamp"].max()
    since = (end - timedelta(days=WINDOW_DAYS)).normalize() + timedelta(days=1)
    win = df[df["timestamp"] >= since]
    weeks = WINDOW_DAYS / 7
    mine_g = win[win["ev_id"] == ev_id]
    mine = _metrics(mine_g, weeks)
    peers = [_metrics(g, weeks) for e, g in win.groupby("ev_id") if e != ev_id]

    def energy_between(t0, t1):
        g = df[(df["ev_id"] == ev_id) & (df["timestamp"] > t0) & (df["timestamp"] <= t1)]
        return float(g["charging_kw"].sum()), float(g["_cost"].sum())

    drive = _driving(odometer or [], energy_between, since)
    compare = {k: _compare(mine[k], [p[k] for p in peers], hi) for k, hi in
               (("per_week", "neutral"), ("avg_kwh", "neutral"), ("peak_share_pct", "worse"), ("won_per_kwh", "worse"))}

    insights = []
    c = compare["per_week"]
    if c and mine["per_week"] is not None:
        ratio = mine["per_week"] / c["median"] if c["median"] else 1.0
        kind = "자주 조금씩 충전하는 편" if ratio >= 1.2 else "가끔 몰아서 충전하는 편" if ratio <= 0.8 else "비슷한 빈도로 충전하는 편"
        insights.append({"title": "충전 습관", "text": f"주 {mine['per_week']}회, 1회 평균 {mine['avg_kwh'] or 0}kWh — 다른 서포터즈(중앙값 주 {c['median']}회)와 비교하면 {kind}이에요."})
    c = compare["peak_share_pct"]
    if c and mine["peak_share_pct"] is not None:
        better = mine["peak_share_pct"] <= (c["median"] or 0)
        insights.append({"title": "전력 피크 시간 충전", "text": f"충전량의 {mine['peak_share_pct']}%를 전력수요가 많은 시간에 충전했어요(다른 서포터즈 중앙값 {c['median']}%). "
                         + ("피크를 잘 피하고 있어요." if better else "충전 시간 추천을 따르면 이 비중을 줄일 수 있어요.")})
    daily_kwh = (mine["kwh_per_week"] or 0) / 7
    soc_now = mine_g.sort_values("timestamp")["soc_pct"].iloc[-1] if not mine_g.empty else None
    suggestion = None
    if daily_kwh > 0 and battery_kwh:
        floor_pct = max(MIN_SOC_FLOOR_PCT, min(90.0, daily_kwh * DAILY_USE_FACTOR / battery_kwh * 100 + MIN_SOC_MARGIN_PCT))
        suggestion = round(floor_pct / 5) * 5
        if soc_now is not None:
            usable = max(0.0, (float(soc_now) - (min_soc_pct or suggestion)) / 100 * battery_kwh)
            insights.append({"title": "지금 배터리로", "text": f"하루 평균 {daily_kwh:.1f}kWh를 쓰니, 지금 배터리({float(soc_now):.0f}%)로 최소 보장 배터리까지 약 {usable / daily_kwh:.1f}일 운행할 수 있어요."})
        cur = f"지금 설정은 {min_soc_pct:.0f}%예요." if min_soc_pct is not None else "아직 설정하지 않았어요."
        insights.append({"title": "최소 보장 배터리 제안", "text": f"하루 운행분의 1.5배에 여유 10%를 더하면 {suggestion}%가 알맞아요. {cur} 이 값 아래로는 방전(V2G)을 하지 않아요."})
    if target_soc_pct is not None and daily_kwh > 0 and battery_kwh:
        days_cover = (target_soc_pct - (min_soc_pct or MIN_SOC_FLOOR_PCT)) / 100 * battery_kwh / daily_kwh
        gap_days = 7 / mine["per_week"] if mine["per_week"] else None
        insights.append({"title": "목표 배터리 점검", "text": f"목표 {target_soc_pct:.0f}%까지 채우면 약 {days_cover:.1f}일 운행분이에요."
                         + (f" 평소 충전 간격(약 {gap_days:.1f}일)보다 넉넉해요." if gap_days and days_cover >= gap_days
                            else f" 평소 충전 간격(약 {gap_days:.1f}일)보다 짧아요 — 목표를 조금 높여 보세요." if gap_days else "")})
    if drive["won_per_km"] is not None:
        insights.append({"title": "1km당 충전 요금", "text": f"최근 기록 기준 1km에 {drive['won_per_km']}원, 1kWh로 {drive['km_per_kwh']}km를 달렸어요."})
    elif drive["entries"] < 2:
        insights.append({"title": "주행거리 기록", "text": "누적 주행거리를 2번 이상 기록하면 주간 주행거리·전비·1km당 요금도 계산해 드려요."})

    return {
        "has_data": True, "ev_id": ev_id, "window": {"from": since.date().isoformat(), "to": end.date().isoformat(), "days": WINDOW_DAYS},
        "metrics": {k: v for k, v in mine.items() if not k.startswith("_")},
        "driving": drive, "compare": compare, "peer_count": len(peers),
        "min_soc_suggestion_pct": suggestion, "insights": insights,
        "generation": "measured_aggregate",
        "note": "차량 기록을 집계한 값이에요. 요금은 모의 단가 기준이라 실제 청구액과 다를 수 있어요.",
    }
