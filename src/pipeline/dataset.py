"""실증 데이터셋(결합 레코드) — 평가의견 4·14 보완(2026-11-23 결정).

1건 = 유효 이벤트 1회(차량 운행 1회 · 차량 충전 1회 · 거점 충전 1회)에 대해
  ① 차량·충전기 결합 블록(차량 상태 + 운행/충전 이벤트, 서포터즈 충전이 연계 충전기에서 일어났으면 그 충전기 세션도 결합)
  ② AI 학습 블록(이벤트 시작 시각이 속한 1시간의 외부환경 — 전력수요·재생에너지·기온·전력 가격·공휴일)
을 결합한 레코드 1개. ①의 필수 항목을 갖추고 ②가 결합된 레코드만 성과 건수(counted)로 센다.

A 서포터즈 결합형 = 차량 OBD 운행·충전 이벤트(ev_id 기준)
B 거점 결합형     = 연계 충전기의 유효 세션 중 서포터즈 차량 세션을 뺀 것(서포터즈 세션은 A형 충전에 결합돼 한 번만 센다)
"""
from __future__ import annotations

import pandas as pd

from src import kpi

CONTEXT_COLS = ["power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh", "is_holiday"]
LINK_WINDOW = pd.Timedelta(hours=1)   # 서포터즈 충전 이벤트와 충전기 세션을 같은 충전으로 보는 시작 시각 차이


def _vehicle_events(ev_df: pd.DataFrame) -> pd.DataFrame:
    t = kpi.vehicle_session_table(ev_df)
    if t.empty:
        return t.assign(source="A")
    meta = {}
    for col in ("vehicle_model", "battery_capacity_kwh", "v2g_capable"):
        if ev_df is not None and col in ev_df.columns:
            meta[col] = ev_df.groupby("ev_id")[col].first()
    out = t.assign(source="A")
    for col, s in meta.items():
        out[col] = out["ev_id"].map(s)
    # ① 차량 블록 필수: 차량 ID·시작 시각·시작 SOC
    out["block1_ok"] = out["ev_id"].notna() & out["start_ts"].notna() & out["soc_start_pct"].notna()
    return out


def _link_chargers(vehicle: pd.DataFrame, charger_df: pd.DataFrame | None) -> pd.DataFrame:
    """서포터즈 충전 이벤트에 연계 충전기 세션(같은 차량, 시작 시각 ±1시간)을 붙인다."""
    vehicle = vehicle.assign(charger_id=None, site_id=None, charger_session_id=None, charger_energy_kwh=None)
    if charger_df is None or charger_df.empty or "ev_id" not in charger_df.columns or vehicle.empty:
        return vehicle
    ch = charger_df[charger_df["ev_id"].notna()].copy()
    if ch.empty:
        return vehicle
    ch["_start"] = pd.to_datetime(ch["start_ts"], errors="coerce").astype("datetime64[ns]")
    ch["ev_id"] = ch["ev_id"].astype(str)
    ch = ch.dropna(subset=["_start"]).sort_values("_start")
    charges = vehicle[vehicle["kind"] == "charge"].reset_index().sort_values("start_ts")
    if charges.empty:
        return vehicle
    charges["start_ts"] = charges["start_ts"].astype("datetime64[ns]")
    charges["ev_id"] = charges["ev_id"].astype(str)
    merged = pd.merge_asof(charges, ch[["ev_id", "_start", "charger_id", "site_id", "session_id", "energy_kwh"]
                                       if "site_id" in ch.columns else ["ev_id", "_start", "charger_id", "session_id", "energy_kwh"]]
                           .rename(columns={"session_id": "_csid", "energy_kwh": "_cekwh", "charger_id": "_cid", "site_id": "_sid"}),
                           left_on="start_ts", right_on="_start", by="ev_id", direction="nearest", tolerance=LINK_WINDOW)
    idx = merged["index"]
    vehicle.loc[idx, "charger_id"] = merged["_cid"].to_numpy()
    if "_sid" in merged.columns:
        vehicle.loc[idx, "site_id"] = merged["_sid"].to_numpy()
    vehicle.loc[idx, "charger_session_id"] = merged["_csid"].to_numpy()
    vehicle.loc[idx, "charger_energy_kwh"] = merged["_cekwh"].to_numpy()
    return vehicle


def _site_events(charger_df: pd.DataFrame | None, supporter_ids: set) -> pd.DataFrame:
    f = kpi.session_funnel(charger_df, supporter_ids) if charger_df is not None else {"_rows": pd.DataFrame()}
    rows = f["_rows"]
    if rows is None or rows.empty:
        return pd.DataFrame(columns=["source", "kind", "start_ts"])
    out = pd.DataFrame({
        "source": "B",
        "kind": "site_charge",
        "ev_id": None,
        "start_ts": rows["_start"].to_numpy(),
        "end_ts": pd.to_datetime(rows["end_ts"], errors="coerce").to_numpy() if "end_ts" in rows.columns else pd.NaT,
        "energy_kwh": pd.to_numeric(rows["energy_kwh"], errors="coerce").to_numpy(),
        "charger_id": rows["charger_id"].to_numpy(),
        "charger_session_id": rows["session_id"].to_numpy() if "session_id" in rows.columns else None,
    })
    for col in ("site_id", "operator_id", "charger_type", "soc_start_pct", "soc_end_pct", "vehicle_model"):
        if col in rows.columns:
            out[col] = rows[col].to_numpy()
    # ① 충전 블록 필수(충전기 ID·시작 시각·충전량>0)는 session_funnel이 이미 걸렀다
    out["block1_ok"] = True
    out["vehicle_info"] = out["soc_start_pct"].notna() if "soc_start_pct" in out.columns else False
    return out


def _join_context(events: pd.DataFrame, grid_df: pd.DataFrame | None) -> pd.DataFrame:
    if events.empty:
        return events.assign(context_ok=pd.Series(dtype=bool))
    hour = pd.to_datetime(events["start_ts"]).dt.floor("h")
    if grid_df is None or grid_df.empty or "timestamp" not in grid_df.columns:
        return events.assign(context_ok=False)
    cols = [c for c in CONTEXT_COLS if c in grid_df.columns]
    g = grid_df[["timestamp"] + cols].copy()
    g["timestamp"] = pd.to_datetime(g["timestamp"]).dt.floor("h")
    g = g.drop_duplicates("timestamp").set_index("timestamp")
    ctx = g.reindex(hour.to_numpy())
    ctx.index = events.index
    ok = ctx[cols].notna().all(axis=1) if len(cols) == len(CONTEXT_COLS) else pd.Series(False, index=events.index)
    return pd.concat([events, ctx], axis=1).assign(context_ok=ok.to_numpy())


def build_dataset(ev_df: pd.DataFrame, charger_df: pd.DataFrame | None, grid_df: pd.DataFrame | None) -> pd.DataFrame:
    """결합 레코드 표. counted = ①필수 충족 ∧ ②결합 성공. project_year = 시작 시각 기준 연차."""
    supporters = kpi._supporter_ids(ev_df)
    vehicle = _link_chargers(_vehicle_events(ev_df), charger_df)
    site = _site_events(charger_df, supporters)
    events = pd.concat([d for d in (vehicle, site) if not d.empty], ignore_index=True) if (not vehicle.empty or not site.empty) \
        else pd.DataFrame(columns=["source", "kind", "start_ts", "block1_ok"])
    events = _join_context(events, grid_df)
    if events.empty:
        return events.assign(counted=pd.Series(dtype=bool), project_year=pd.Series(dtype=object))
    events["block1_ok"] = events["block1_ok"].fillna(False).astype(bool)
    events["counted"] = events["block1_ok"] & events["context_ok"].astype(bool)
    events["project_year"] = pd.to_datetime(events["start_ts"]).dt.date.map(kpi.project_year_of)
    return events.sort_values("start_ts").reset_index(drop=True)


def summarize(ds: pd.DataFrame, plan: dict | None = None) -> dict:
    """이벤트 종류별 건수, ①·② 충족 건수, 결합 성공률, 연차별 성과 건수, 계획 대비."""
    plan = plan or kpi.DATA_PLAN
    if ds is None or ds.empty:
        return {"events": 0, "counted": 0, "by_kind": {}, "join_rate_pct": None, "by_year": {}, "plan": plan}
    b1 = ds[ds["block1_ok"]]
    join = round(float(b1["context_ok"].mean()) * 100, 1) if len(b1) else None
    by_kind = {k: {"events": int((ds["kind"] == k).sum()), "counted": int(((ds["kind"] == k) & ds["counted"]).sum())}
               for k in ("drive", "charge", "site_charge")}
    linked = int(((ds["kind"] == "charge") & ds["charger_id"].notna()).sum()) if "charger_id" in ds.columns else 0
    site_vehicle_info = int(ds.loc[ds["kind"] == "site_charge", "vehicle_info"].fillna(False).astype(bool).sum()) \
        if "vehicle_info" in ds.columns else 0
    by_year = {}
    for key, y in kpi.PROJECT_YEARS.items():
        n = int((ds["counted"] & (ds["project_year"] == key)).sum())
        by_year[key] = {"label": y["label"], "counted": n, "target": y["target"], "achieved_pct": round(n / y["target"] * 100, 1)}
    return {
        "definition": "1건 = 유효 이벤트 1회(차량 운행·차량 충전·거점 충전) × [① 차량·충전기 결합 블록 + ② AI 학습 블록]",
        "events": int(len(ds)),
        "block1_ok": int(len(b1)),
        "counted": int(ds["counted"].sum()),
        "join_rate_pct": join,
        "min_join_rate_pct": plan.get("min_join_rate_pct"),
        "join_rate_ok": None if join is None or plan.get("min_join_rate_pct") is None else join >= plan["min_join_rate_pct"],
        "by_kind": by_kind,
        "supporter_charges_linked_to_charger": linked,
        "site_sessions_with_vehicle_info": site_vehicle_info,
        "by_year": by_year,
        "plan": plan,
    }
