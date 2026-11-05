"""서포터즈 '내 충전 리포트' — 최근 4주 충전·주행 지표, 다른 실증 차량과의 비교, 조합 인사이트(AIV2GAPP F.O 리포트 이식).

계획서 4.1 라 "EV별 AI 추천 충전 시간 추론 및 산출, 결과 근거를 설명 형태로 제공"의 근거 화면이다.
정해진 산식과 문장 틀로만 설명한다.

- 충전 지표는 차량 시간별 기록(ev_telemetry)에서 계산한다: 충전 중인 시간이 이어진 구간 = 충전 1회, 그 시간의 kW 합 = 충전량.
- 주행 지표는 서포터즈가 입력한 누적 주행거리(odometer_logs)가 2번 이상 있을 때만 계산한다(차량 기록에 주행거리가 없다).
- 요금은 계통 신호의 시간별 단가(price_krw_per_kwh, 모의·가정값일 수 있음), 피크 시간대는 효용성 검증과 같은 정의
  (날짜별 전력수요 상위 25%)를 쓴다.
- 비교군은 같은 기간의 다른 실증 차량 전체의 중앙값이다(비교군을 가상으로 만들지 않는다).
- 내 차 AI 적용 전·후와 V2G 기여는 대시보드 효용성 검증과 같은 최적화(run_v2g_optimization)를 내 차 4주 기록에만 돌린
  추론값이다. 이익공유액은 절감액 × 차주 배분 비율(가정 50%)의 모의값이며 실제 지급이 아니다(계획서: 보상은 2차년도 설계).
"""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from src.automation import issues
from src.datagen.config import SimConfig
from src.optimizer.economics import EconomicsParams
from src.optimizer.effect_metrics import compute_effect_metrics, peak_hours
from src.optimizer.poc_compare import aggregate_comparison, run_v2g_optimization

WINDOW_DAYS = 28
MIN_SOC_FLOOR_PCT = 30.0      # 최소 보장 배터리 제안의 하한(AIV2GAPP B.O 기본값)
MIN_SOC_MARGIN_PCT = 10.0     # 하루 운행분 × 1.5에 더하는 여유
DAILY_USE_FACTOR = 1.5
OWNER_SHARE_PCT = 50.0        # 이익공유 모의: 절감액 중 차주 몫(가정 — 대시보드 배분 시험의 기본값과 같다)
HISTORY_DAYS = 182            # 충전 기록 그래프(일 14칸·주 8칸·월 6칸)용 일별 집계 기간


def _r1(x):
    return None if x is None or pd.isna(x) else round(float(x), 1)


def _episodes(g: pd.DataFrame) -> pd.DataFrame:
    """충전 중인 시간이 이어진 구간 = 충전 1회. 반환: (start, kwh, peak_kwh, cost_krw)."""
    g = g.sort_values("timestamp")
    on = g["charging_kw"] > 0.05
    if not on.any():
        return pd.DataFrame(columns=["start", "end", "kwh", "peak_kwh", "cost_krw"])
    gap = g["timestamp"].diff().ne(pd.Timedelta(hours=1)) | ~on.shift(fill_value=False)
    run = (gap & on).cumsum()
    c = g[on].assign(_run=run[on])
    return c.groupby("_run").agg(start=("timestamp", "min"), end=("timestamp", "max"), kwh=("charging_kw", "sum"),
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


def _my_effect(ev_rows: pd.DataFrame, grid_df: pd.DataFrame, target_soc_pct: float | None) -> dict | None:
    """내 차 최근 4주 기록에 AI 최적화를 적용한 적용 전·후 비교와 V2G 기여(추론). 계산할 충전 구간이 없으면 None."""
    if ev_rows.empty or grid_df is None or grid_df.empty:
        return None
    economics = EconomicsParams.load()
    target = (target_soc_pct or 90.0) / 100
    try:
        run = run_v2g_optimization(ev_rows, grid_df, SimConfig(), target_soc=target, economics=economics)
    except Exception as exc:   # noqa: BLE001 — 최적화가 안 풀려도 리포트의 나머지는 보여 준다
        ev = str(ev_rows["ev_id"].iloc[0]) if "ev_id" in ev_rows else None
        issues.record("optimize", f"내 충전 리포트 적용 전·후 최적화 실패: {type(exc).__name__}",
                      detail={"ev_id": ev, "error": str(exc)[:200]})
        return None
    if not run["sessions"]:
        return None
    comparison = aggregate_comparison(ev_rows, run["schedule"])
    m = compute_effect_metrics(run, comparison, ev_rows, grid_df, target, economics)
    cost = m["cost"]
    savings = max(0.0, float(cost["savings_krw"]))
    return {
        "sessions": cost["sessions"],
        "peak_share_pct": {"before": m["peak"]["baseline_peak_charge_share_pct"], "after": m["peak"]["optimized_peak_charge_share_pct"]},
        "target_reach_pct": {"before": m["target_reach"]["baseline_reach_pct"], "after": m["target_reach"]["optimized_reach_pct"]},
        "cost_krw": {"before": cost["baseline_cost_krw"], "after": cost["optimized_cost_krw"]},
        "savings_krw": round(savings),
        "discharge_kwh": cost["discharge_kwh"],
        "share": {"owner_pct": OWNER_SHARE_PCT, "owner_krw": round(savings * OWNER_SHARE_PCT / 100), "simulated": True},
        "generation": "inferred",
    }


def _recent(ep: pd.DataFrame, n: int = 3) -> list[dict]:
    rows = ep.sort_values("start", ascending=False).head(n)
    return [{"start": r.start.isoformat(), "end": (r.end + pd.Timedelta(hours=1)).isoformat(), "kwh": round(float(r.kwh), 1),
             "cost_krw": round(float(r.cost_krw)), "won_per_kwh": round(float(r.cost_krw) / float(r.kwh)) if r.kwh > 0 else None}
            for r in rows.itertuples()]


def _daily(ep: pd.DataFrame, end, days: int = HISTORY_DAYS) -> list[dict]:
    if ep.empty:
        return []
    since = (pd.Timestamp(end).normalize() - pd.Timedelta(days=days - 1))
    e = ep[ep["start"] >= since].assign(_d=lambda x: x["start"].dt.date)
    g = e.groupby("_d").agg(n=("kwh", "size"), kwh=("kwh", "sum"), cost=("cost_krw", "sum"))
    return [{"date": d.isoformat(), "sessions": int(r.n), "kwh": round(float(r.kwh), 1), "cost_krw": round(float(r.cost))}
            for d, r in g.iterrows()]


# 설명 문장 틀(한국어·영어) — 숫자만 채워 넣는다
TEXT = {
    "ko": {
        "no_data": "아직 내 차량의 충전 기록이 없어요. OBD가 연결되고 기록이 쌓이면 리포트가 만들어져요.",
        "habit_t": "충전 습관", "habit": "주 {per_week}회, 1회 평균 {avg_kwh}kWh — 다른 서포터즈(중앙값 주 {median}회)와 비교하면 {kind}이에요.",
        "often": "자주 조금씩 충전하는 편", "rarely": "가끔 몰아서 충전하는 편", "similar": "비슷한 빈도로 충전하는 편",
        "peak_t": "전력 피크 시간 충전", "peak": "충전량의 {share}%를 전력수요가 많은 시간에 충전했어요(다른 서포터즈 중앙값 {median}%). ",
        "peak_good": "피크를 잘 피하고 있어요.", "peak_bad": "충전 시간 추천을 따르면 이 비중을 줄일 수 있어요.",
        "now_t": "지금 배터리로", "now": "하루 평균 {daily:.1f}kWh를 쓰니, 지금 배터리({soc:.0f}%)로 최소 보장 배터리까지 약 {days:.1f}일 운행할 수 있어요.",
        "min_t": "최소 보장 배터리 제안", "min": "하루 운행분의 1.5배에 여유 10%를 더하면 {pct}%가 알맞아요. {cur} 이 값 아래로는 방전(V2G)을 하지 않아요.",
        "min_cur": "지금 설정은 {pct:.0f}%예요.", "min_unset": "아직 설정하지 않았어요.",
        "target_t": "목표 배터리 점검", "target": "목표 {pct:.0f}%까지 채우면 약 {days:.1f}일 운행분이에요.",
        "target_ok": " 평소 충전 간격(약 {gap:.1f}일)보다 넉넉해요.", "target_low": " 평소 충전 간격(약 {gap:.1f}일)보다 짧아요 — 목표를 조금 높여 보세요.",
        "km_t": "1km당 충전 요금", "km": "최근 기록 기준 1km에 {won}원, 1kWh로 {km}km를 달렸어요.",
        "odo_t": "주행거리 기록", "odo": "누적 주행거리를 2번 이상 기록하면 주간 주행거리·전비·1km당 요금도 계산해 드려요.",
        "note": "차량 기록을 집계한 값이에요. 요금은 모의 단가 기준이라 실제 청구액과 다를 수 있어요.",
    },
    "en": {
        "no_data": "There are no charging records for your car yet. The report appears once the OBD device is connected and data builds up.",
        "habit_t": "Charging habit", "habit": "{per_week} charges a week, {avg_kwh} kWh on average. Compared with other supporters (median {median} a week), you {kind}.",
        "often": "charge little and often", "rarely": "charge less often but more at a time", "similar": "charge about as often",
        "peak_t": "Charging at peak hours", "peak": "{share}% of your charging happened when city power demand was high (median of other supporters: {median}%). ",
        "peak_good": "You are avoiding the peak well.", "peak_bad": "Following the recommended charging times can lower this share.",
        "now_t": "With your current battery", "now": "You use about {daily:.1f} kWh a day, so your current battery ({soc:.0f}%) lasts about {days:.1f} days before reaching the minimum battery.",
        "min_t": "Suggested minimum battery", "min": "Your daily driving x 1.5 plus a 10% margin suggests {pct}%. {cur} No discharge (V2G) happens below this level.",
        "min_cur": "Your current setting is {pct:.0f}%.", "min_unset": "You have not set it yet.",
        "target_t": "Target battery check", "target": "Charging to {pct:.0f}% covers about {days:.1f} days of driving.",
        "target_ok": " That is more than your usual charging interval (about {gap:.1f} days).", "target_low": " That is shorter than your usual charging interval (about {gap:.1f} days). Consider a higher target.",
        "km_t": "Charging cost per km", "km": "Based on recent entries you paid {won} KRW per km and drove {km} km per kWh.",
        "odo_t": "Odometer entries", "odo": "Log your odometer twice or more and we will also calculate weekly distance, efficiency and cost per km.",
        "note": "Aggregated from your car's records. Costs use simulated prices and may differ from actual bills.",
    },
}


def build_report(ev_id: str, ev_df: pd.DataFrame, grid_df: pd.DataFrame, battery_kwh: float,
                 odometer: list[dict] | None = None, min_soc_pct: float | None = None,
                 target_soc_pct: float | None = None, lang: str = "ko") -> dict:
    """ev_id 차량의 최근 4주 리포트. 그 차량 기록이 없으면 has_data=False. lang은 ko|en(설명 문장 언어)."""
    L = TEXT["en" if lang == "en" else "ko"]
    if ev_df is None or ev_df.empty or ev_id not in set(ev_df["ev_id"]):
        return {"has_data": False, "ev_id": ev_id, "message": L["no_data"]}
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

    def add(key, text):
        insights.append({"title": L[key + "_t"], "text": text})

    c = compare["per_week"]
    if c and mine["per_week"] is not None:
        ratio = mine["per_week"] / c["median"] if c["median"] else 1.0
        kind = L["often"] if ratio >= 1.2 else L["rarely"] if ratio <= 0.8 else L["similar"]
        add("habit", L["habit"].format(per_week=mine["per_week"], avg_kwh=mine["avg_kwh"] or 0, median=c["median"], kind=kind))
    c = compare["peak_share_pct"]
    if c and mine["peak_share_pct"] is not None:
        better = mine["peak_share_pct"] <= (c["median"] or 0)
        add("peak", L["peak"].format(share=mine["peak_share_pct"], median=c["median"]) + (L["peak_good"] if better else L["peak_bad"]))
    daily_kwh = (mine["kwh_per_week"] or 0) / 7
    soc_now = mine_g.sort_values("timestamp")["soc_pct"].iloc[-1] if not mine_g.empty else None
    suggestion = None
    if daily_kwh > 0 and battery_kwh:
        floor_pct = max(MIN_SOC_FLOOR_PCT, min(90.0, daily_kwh * DAILY_USE_FACTOR / battery_kwh * 100 + MIN_SOC_MARGIN_PCT))
        suggestion = round(floor_pct / 5) * 5
        if soc_now is not None:
            usable = max(0.0, (float(soc_now) - (min_soc_pct or suggestion)) / 100 * battery_kwh)
            add("now", L["now"].format(daily=daily_kwh, soc=float(soc_now), days=usable / daily_kwh))
        cur = L["min_cur"].format(pct=min_soc_pct) if min_soc_pct is not None else L["min_unset"]
        add("min", L["min"].format(pct=suggestion, cur=cur))
    if target_soc_pct is not None and daily_kwh > 0 and battery_kwh:
        days_cover = (target_soc_pct - (min_soc_pct or MIN_SOC_FLOOR_PCT)) / 100 * battery_kwh / daily_kwh
        gap = 7 / mine["per_week"] if mine["per_week"] else None
        tail = "" if not gap else (L["target_ok"] if days_cover >= gap else L["target_low"]).format(gap=gap)
        add("target", L["target"].format(pct=target_soc_pct, days=days_cover) + tail)
    if drive["won_per_km"] is not None:
        add("km", L["km"].format(won=drive["won_per_km"], km=drive["km_per_kwh"]))
    elif drive["entries"] < 2:
        add("odo", L["odo"])

    mine_all = df[df["ev_id"] == ev_id]
    episodes_all = _episodes(mine_all)
    return {
        "has_data": True, "ev_id": ev_id, "window": {"from": since.date().isoformat(), "to": end.date().isoformat(), "days": WINDOW_DAYS},
        "as_of": end.date().isoformat(),
        "soc_now_pct": None if soc_now is None or pd.isna(soc_now) else round(float(soc_now), 1),
        "recent": _recent(episodes_all),
        "daily": _daily(episodes_all, end),
        "effect": _my_effect(ev_df[(ev_df["ev_id"] == ev_id) & (pd.to_datetime(ev_df["timestamp"]) >= since)], grid_df, target_soc_pct),
        "metrics": {k: v for k, v in mine.items() if not k.startswith("_")},
        "driving": drive, "compare": compare, "peer_count": len(peers),
        "min_soc_suggestion_pct": suggestion, "insights": insights,
        "generation": "measured_aggregate", "note": L["note"],
    }
