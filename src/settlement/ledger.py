"""가상 전력 거래 및 모의 정산(포인트) 시스템 PoC (신구대조표 2번 · 계획서 p.30~31, 요구사항 E03·Y2D03·Y2E04).

흐름
  1) 거래 실적 산정: 참여 차량별·일별로 피크 시간대 방전량(kWh)과 과잉발전 흡수량(kWh)을 계측(또는 추론)해 기록한다.
  2) 포인트 산정: 피크 방전 kWh x 그 시각 보상단가 x 참여자 배분율 + 흡수 kWh x 보너스 (config/vpp_settlement.json)
  3) 원장 기록: 거래(virtual_trades)마다 적립(earn) 한 줄을 포인트 원장(point_ledger)에 남긴다. 잔액 = 원장 합계.
  4) 조회: 참여자는 모바일에서 잔액·내역을, 관리자는 관용/민간별 집계를 본다.

원칙
- 같은 차량·같은 날·같은 생성방식(measured/inferred)은 한 번만 정산한다(UNIQUE) — 다시 돌려도 중복 적립되지 않는다.
- 1차년도는 물리 방전 인프라가 없어 방전량은 최적 스케줄 기반 '추론(inferred)'이다. 원장·화면에 그대로 표기하고,
  실제 금전 지급이 아닌 모의 포인트다(1포인트 = 1원 상당 표시용).
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from src.pipeline import store

AFFILIATION_LABELS = {"public": "관용", "private": "민간"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def compute_trades(run: dict, ev_df: pd.DataFrame, curtailment: dict, params: dict, affiliation_of) -> pd.DataFrame:
    """차량·일 단위 거래 실적과 포인트를 계산한다(DB에 쓰지 않음).

    run: run_v2g_optimization 결과(schedule, session_ev_map, sell_price_by_time)
    curtailment: curtailment.emulate 결과(_hourly.absorb_kw — 시간별 선단 흡수량을 그 시각 연결 차량에 균등 배분)
    affiliation_of: ev_id -> ('public'|'private', 출처 문자열)
    """
    pts = params["points"]
    peak_hours = set(params["curtailment"]["peak_hours"])
    sell = run.get("sell_price_by_time") or run.get("price_by_time") or {}
    discharge = defaultdict(float)   # (ev_id, date) -> kWh (피크 시간대)
    value = defaultdict(float)
    for sid, sched in run["schedule"].items():
        ev_id = run["session_ev_map"].get(sid)
        if ev_id is None:
            continue
        for t, (_c, d) in sched.items():
            if d <= 0 or pd.Timestamp(t).hour not in peak_hours:
                continue
            key = (ev_id, pd.Timestamp(t).normalize())
            discharge[key] += d
            value[key] += d * float(sell.get(t, 0.0))

    # 에뮬레이터의 선단 시간별 흡수(과잉발전 시간)·피크 방전(저녁 피크)을 그 시각 연결 중인 차량에 균등 배분한다
    absorb = defaultdict(float)
    hourly = curtailment["_hourly"]
    conn = ev_df[ev_df["connected"]][["timestamp", "ev_id"]].copy()
    conn["timestamp"] = pd.to_datetime(conn["timestamp"])
    active = hourly[(hourly["absorb_kw"] > 0) | (hourly["peak_discharge_kw"] > 0)]
    if not active.empty and not conn.empty:
        merged = conn.merge(active[["timestamp", "absorb_kw", "peak_discharge_kw", "price_krw_per_kwh"]], on="timestamp")
        if not merged.empty:
            n = merged.groupby("timestamp")["ev_id"].transform("count")
            merged["a"] = merged["absorb_kw"] / n
            merged["d"] = merged["peak_discharge_kw"] / n
            merged["v"] = merged["d"] * merged["price_krw_per_kwh"]
            day = merged["timestamp"].dt.normalize()
            for (ev_id, dd), r in merged.groupby(["ev_id", day])[["a", "d", "v"]].sum().iterrows():
                absorb[(ev_id, dd)] += float(r["a"])
                discharge[(ev_id, dd)] += float(r["d"])
                value[(ev_id, dd)] += float(r["v"])

    rows = []
    for key in set(discharge) | set(absorb):
        ev_id, day = key
        d_kwh, a_kwh = discharge.get(key, 0.0), absorb.get(key, 0.0)
        if d_kwh + a_kwh < pts["min_settle_kwh"]:
            continue
        v = value.get(key, 0.0)
        points = round(v * pts["owner_share_pct"] / 100 / pts["krw_per_point"] + a_kwh * pts["absorb_bonus_points_per_kwh"])
        aff, aff_src = affiliation_of(ev_id)
        rows.append({
            "ev_id": ev_id, "trade_date": day.strftime("%Y-%m-%d"), "affiliation": aff, "affiliation_source": aff_src,
            "peak_discharge_kwh": round(d_kwh, 3), "absorb_kwh": round(a_kwh, 3), "value_krw": round(v, 0), "points": int(points),
        })
    return pd.DataFrame(rows, columns=["ev_id", "trade_date", "affiliation", "affiliation_source", "peak_discharge_kwh",
                                       "absorb_kwh", "value_krw", "points"]).sort_values(["trade_date", "ev_id"]).reset_index(drop=True)


def settle(trades: pd.DataFrame, generation: str, data_source: str, con: sqlite3.Connection | None = None) -> dict:
    """거래를 원장에 기록한다. 이미 정산된 (차량, 날짜, 생성방식)은 건너뛴다."""
    if generation not in ("measured", "inferred"):
        raise ValueError("generation은 measured 또는 inferred")
    own = con is None
    con = con or store.get_connection()
    inserted = skipped = points = 0
    try:
        now = _now()
        for r in trades.to_dict("records"):
            cur = con.execute(
                "INSERT OR IGNORE INTO virtual_trades (ev_id, trade_date, affiliation, peak_discharge_kwh, absorb_kwh, value_krw, "
                "points, generation, data_source, settled_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["ev_id"], r["trade_date"], r["affiliation"], r["peak_discharge_kwh"], r["absorb_kwh"], r["value_krw"],
                 r["points"], generation, data_source, now))
            if cur.rowcount != 1:
                skipped += 1
                continue
            con.execute(
                "INSERT INTO point_ledger (ev_id, kind, points, trade_id, memo, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
                (r["ev_id"], "earn", r["points"], cur.lastrowid, f"{r['trade_date']} 가상 전력 거래 정산", now, "settlement"))
            inserted += 1
            points += r["points"]
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        if own:
            con.close()
    return {"inserted": inserted, "skipped_already_settled": skipped, "points_issued": points}


def balance(ev_id: str, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or store.get_connection()
    try:
        return int(con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0])
    finally:
        if own:
            con.close()


def owner_statement(ev_id: str, limit: int = 30, con: sqlite3.Connection | None = None) -> dict:
    """참여자 본인 포인트 잔액·최근 거래 내역(모바일)."""
    own = con is None
    con = con or store.get_connection()
    try:
        rows = con.execute(
            "SELECT t.trade_date, t.peak_discharge_kwh, t.absorb_kwh, t.value_krw, t.points, t.generation "
            "FROM virtual_trades t WHERE t.ev_id=? ORDER BY t.trade_date DESC LIMIT ?", (ev_id, limit)).fetchall()
        agg = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(peak_discharge_kwh),0), COALESCE(SUM(absorb_kwh),0) FROM virtual_trades WHERE ev_id=?",
            (ev_id,)).fetchone()
        return {
            "ev_id": ev_id,
            "balance_points": balance(ev_id, con),
            "trade_days": int(agg[0]),
            "total_peak_discharge_kwh": round(float(agg[1]), 2),
            "total_absorb_kwh": round(float(agg[2]), 2),
            "history": [
                {"date": r[0], "peak_discharge_kwh": r[1], "absorb_kwh": r[2], "value_krw": r[3], "points": r[4],
                 "generation": r[5], "generation_label": "실측" if r[5] == "measured" else "추론(모의)"}
                for r in rows
            ],
            "note": "모의 포인트입니다(실제 금전 지급 아님). 1차년도 방전량은 최적 스케줄 기반 추론값입니다.",
        }
    finally:
        if own:
            con.close()


def summary(con: sqlite3.Connection | None = None) -> dict:
    """관리자 집계 — 관용/민간별 참여 차량·거래일·kWh·포인트, 거래·정산 가능범위(Y2E04)."""
    own = con is None
    con = con or store.get_connection()
    try:
        groups = []
        for aff, label in AFFILIATION_LABELS.items():
            r = con.execute(
                "SELECT COUNT(DISTINCT ev_id), COUNT(*), COALESCE(SUM(peak_discharge_kwh),0), COALESCE(SUM(absorb_kwh),0), "
                "COALESCE(SUM(value_krw),0), COALESCE(SUM(points),0) FROM virtual_trades WHERE affiliation=?", (aff,)).fetchone()
            groups.append({"affiliation": aff, "label": label, "vehicles": r[0], "trade_days": r[1],
                           "peak_discharge_kwh": round(r[2], 1), "absorb_kwh": round(r[3], 1),
                           "value_krw": round(r[4], 0), "points": int(r[5])})
        per_ev = con.execute(
            "SELECT ev_id, affiliation, COUNT(*), SUM(peak_discharge_kwh), SUM(absorb_kwh), SUM(points) FROM virtual_trades "
            "GROUP BY ev_id ORDER BY SUM(points) DESC").fetchall()
        daily_pts = [r[0] for r in con.execute(
            "SELECT SUM(points) FROM virtual_trades GROUP BY ev_id, trade_date").fetchall()]
        rng = con.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT generation) FROM virtual_trades").fetchone()
        gen = dict(con.execute("SELECT generation, COUNT(*) FROM virtual_trades GROUP BY generation").fetchall())
        s = pd.Series(daily_pts, dtype=float)
        return {
            "groups": groups,
            "total_points": int(sum(g["points"] for g in groups)),
            "period": {"from": rng[0], "to": rng[1]},
            "by_generation": gen,
            # 거래·정산 가능범위(레퍼런스 2) — 차량·일 1건당 정산 포인트 분포
            "settlement_range": {
                "per_vehicle_day_points": {"min": int(s.min()) if len(s) else 0, "p50": int(s.median()) if len(s) else 0,
                                           "p90": int(s.quantile(0.9)) if len(s) else 0, "max": int(s.max()) if len(s) else 0},
            },
            "per_vehicle": [{"ev_id": r[0], "affiliation": r[1], "label": AFFILIATION_LABELS.get(r[1], r[1]), "trade_days": r[2],
                             "peak_discharge_kwh": round(r[3], 2), "absorb_kwh": round(r[4], 2), "points": int(r[5])}
                            for r in per_ev],
        }
    finally:
        if own:
            con.close()


def simulate_incentives(trades: pd.DataFrame, params: dict, discount_rate_pct: float = 10.0,
                        revenue_share_pct: float | None = None) -> dict:
    """인센티브 유형별 시뮬레이션(Y2D03·S02 기초) — 같은 거래 실적에 대해
    (a) 포인트 적립, (b) 충전요금 할인(방전 가치의 일정 비율을 다음 충전요금에서 차감), (c) 수익배분(방전 가치 x 배분율)을 비교한다."""
    share = params["points"]["owner_share_pct"] if revenue_share_pct is None else revenue_share_pct
    value = float(trades["value_krw"].sum()) if not trades.empty else 0.0
    absorb = float(trades["absorb_kwh"].sum()) if not trades.empty else 0.0
    n_ev = int(trades["ev_id"].nunique()) if not trades.empty else 0
    pts = float(trades["points"].sum()) if not trades.empty else 0.0
    options = [
        {"type": "points", "label": "모의 포인트 적립", "participant_krw": round(pts * params["points"]["krw_per_point"], 0)},
        {"type": "charge_discount", "label": f"충전요금 할인({discount_rate_pct:.0f}% 상당)",
         "participant_krw": round(value * discount_rate_pct / 100 + absorb * params["points"]["absorb_bonus_points_per_kwh"], 0)},
        {"type": "revenue_share", "label": f"수익배분({share:.0f}%)", "participant_krw": round(value * share / 100, 0)},
    ]
    for o in options:
        o["per_vehicle_krw"] = round(o["participant_krw"] / n_ev, 0) if n_ev else 0
    return {"vehicles": n_ev, "discharge_value_krw": round(value, 0), "options": options, "label": "모의(가정값)"}
