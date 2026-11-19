"""가상 전력 거래 및 모의 정산(리워드) 시스템 PoC — 신구대조표 2번 · 계획서 p.30~31, p.48
(요구사항 E03·S01·Y2D03·Y2E04, 설계서: docs/가상전력거래_모의정산_프로세스_설계서.md).

정산 프로세스
  1) 실적 계측(추론)   차량·일 단위로 피크 시간대 방전량, 과잉발전 흡수량, 요금 절감액을 기록한다.
                        등록 차량은 방전(V2G) 참여 동의가 있을 때만 방전 실적을 인정한다.
  2) 재원별 배분        재원별 편익(요금 차익·피크 저감·출력제어 완화, vpp_revenue)을 기여도로 나눈다.
                          요금 차익   ← 차량의 요금 절감액 비율
                          피크 저감   ← 피크 시간대 방전 kWh 비율
                          출력제어 완화 ← 과잉발전 흡수 kWh 비율
  3) 포인트 산출        (배분액 합계 × 참여자 배분율) ÷ 1포인트 환산액 — 시민 체감형 가상 포인트
  4) 정산 회차          기간을 정해 산정(draft) → 관리자가 검토 후 확정(confirmed, 원장 적립) 또는 취소(cancelled)
  5) 조회·문의·조정      참여자는 확정 포인트·확정 대기·내역을 보고 문의를 남긴다. 관리자는 답변과 함께 포인트를 조정한다.
  6) 경제성 검증        재원 총편익 대비 지급률, 차량당 월 포인트, 참여 유인 기준(보상 기대 수준) 충족 여부

원칙
- 같은 차량·같은 날·같은 생성방식은 한 번만 정산한다(UNIQUE). 취소된 회차의 거래는 지워 다시 산정할 수 있다.
- 1차년도 방전량은 추론값(generation=inferred)이며, 포인트는 모의다 — 실제 금전 지급은 제도 마련 전까지 하지 않는다(p.48).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd

from src.pipeline import store

AFFILIATION_LABELS = {"public": "관용", "private": "민간"}
RUN_STATUS_LABELS = {"draft": "산정(확정 대기)", "confirmed": "확정", "cancelled": "취소"}
TRADE_COLS = ["ev_id", "trade_date", "affiliation", "affiliation_source", "peak_discharge_kwh", "absorb_kwh",
              "saving_krw", "tou_krw", "peak_krw", "curtail_krw", "value_krw", "points", "consent_note"]


class SettlementError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 1)~3) 실적·배분·포인트
def compute_trades(run: dict, ev_df: pd.DataFrame, curtailment: dict, revenue_sources: dict, params: dict,
                   affiliation_of, consent_of=None) -> pd.DataFrame:
    """차량·일 단위 거래 실적과 재원별 배분·포인트를 계산한다(DB에 쓰지 않음).

    consent_of: ev_id -> True(방전 동의) / False(동의 없음) / None(등록부에 없는 데모 차량 — 동의 확인 대상 아님)
    """
    pts = params["points"]
    peak_hours = set(params["curtailment"]["peak_hours"])
    require_consent = pts.get("require_v2g_consent_for_discharge", True)
    price = run.get("price_by_time") or {}
    sell = run.get("sell_price_by_time") or price
    consent_cache: dict = {}

    def consent(ev_id):
        if consent_of is None:
            return None
        if ev_id not in consent_cache:
            consent_cache[ev_id] = consent_of(ev_id)
        return consent_cache[ev_id]

    discharge = defaultdict(float)   # (ev, day) -> 피크 방전 kWh
    saving = defaultdict(float)      # (ev, day) -> 요금 절감액(원) = 일반충전 비용 - 최적 스케줄 비용
    absorb = defaultdict(float)

    # 일반충전 비용(관측 충전) — 차량·일
    ev = ev_df[["timestamp", "ev_id", "charging_kw"]].copy()
    ev["timestamp"] = pd.to_datetime(ev["timestamp"])
    ev["p"] = ev["timestamp"].map(lambda t: float(price.get(t, 0.0)))
    ev["cost"] = ev["charging_kw"] * ev["p"]
    for (ev_id, day), v in ev.groupby(["ev_id", ev["timestamp"].dt.normalize()])["cost"].sum().items():
        saving[(ev_id, day)] += float(v)

    for sid, sched in run["schedule"].items():
        ev_id = run["session_ev_map"].get(sid)
        if ev_id is None:
            continue
        allow_discharge = not (require_consent and consent(ev_id) is False)
        for t, (c, d) in sched.items():
            ts = pd.Timestamp(t)
            key = (ev_id, ts.normalize())
            saving[key] -= (c - (d if allow_discharge else 0.0)) * float(price.get(t, 0.0))
            if d > 0 and allow_discharge and ts.hour in peak_hours:
                discharge[key] += d

    # 에뮬레이터의 흡수(과잉발전 시간)·피크 방전(저녁 피크)을 그 시각 연결 차량에 균등 배분
    hourly = curtailment["_hourly"]
    conn = ev_df[ev_df["connected"]][["timestamp", "ev_id"]].copy()
    conn["timestamp"] = pd.to_datetime(conn["timestamp"])
    active = hourly[(hourly["absorb_kw"] > 0) | (hourly["peak_discharge_kw"] > 0)]
    if not active.empty and not conn.empty:
        merged = conn.merge(active[["timestamp", "absorb_kw", "peak_discharge_kw"]], on="timestamp")
        if not merged.empty:
            n = merged.groupby("timestamp")["ev_id"].transform("count")
            merged["a"] = merged["absorb_kw"] / n
            merged["d"] = merged["peak_discharge_kw"] / n
            for (ev_id, dd), r in merged.groupby(["ev_id", merged["timestamp"].dt.normalize()])[["a", "d"]].sum().iterrows():
                absorb[(ev_id, dd)] += float(r["a"])
                if not (require_consent and consent(ev_id) is False):
                    discharge[(ev_id, dd)] += float(r["d"])

    keys = set(discharge) | set(absorb) | {k for k, v in saving.items() if v > 0}
    pos_saving = {k: max(saving.get(k, 0.0), 0.0) for k in keys}
    tot_s, tot_d, tot_a = sum(pos_saving.values()), sum(discharge.get(k, 0.0) for k in keys), sum(absorb.get(k, 0.0) for k in keys)
    pools = {i["key"]: max(float(i["amount_krw"]), 0.0) for i in revenue_sources["items"]}
    share = pts["owner_share_pct"] / 100

    rows = []
    for key in keys:
        ev_id, day = key
        d_kwh, a_kwh, s_krw = discharge.get(key, 0.0), absorb.get(key, 0.0), pos_saving[key]
        if d_kwh + a_kwh < pts["min_settle_kwh"] and s_krw <= 0:
            continue
        tou = pools.get("tou_arbitrage", 0.0) * (s_krw / tot_s) if tot_s else 0.0
        peak = pools.get("peak_reduction", 0.0) * (d_kwh / tot_d) if tot_d else 0.0
        curt = pools.get("curtailment_relief", 0.0) * (a_kwh / tot_a) if tot_a else 0.0
        value = tou + peak + curt
        aff, aff_src = affiliation_of(ev_id)
        c = consent(ev_id)
        note = "데모 차량(동의 확인 대상 아님)" if c is None else ("방전 동의" if c else "방전 미동의 — 방전 실적 제외")
        rows.append({
            "ev_id": ev_id, "trade_date": day.strftime("%Y-%m-%d"), "affiliation": aff, "affiliation_source": aff_src,
            "peak_discharge_kwh": round(d_kwh, 3), "absorb_kwh": round(a_kwh, 3), "saving_krw": round(s_krw, 0),
            "tou_krw": round(tou, 0), "peak_krw": round(peak, 0), "curtail_krw": round(curt, 0), "value_krw": round(value, 0),
            "points": int(round(value * share / pts["krw_per_point"])), "consent_note": note,
        })
    return pd.DataFrame(rows, columns=TRADE_COLS).sort_values(["trade_date", "ev_id"]).reset_index(drop=True)


# ------------------------------------------------------------------ 6) 경제성 검증
def economics_check(trades: pd.DataFrame, params: dict) -> dict:
    """재원 배분액 대비 참여자 지급률, 차량당 월 포인트, 참여 유인 기준(보상 기대 수준) 충족 여부."""
    pts = params["points"]
    if trades.empty:
        return {"vehicles": 0, "status": "실적 없음"}
    total_value = float(trades["value_krw"].sum())
    paid = float(trades["points"].sum()) * pts["krw_per_point"]
    days = pd.to_datetime(trades["trade_date"])
    months = max((days.max() - days.min()).days + 1, 1) / 30.4
    n_ev = int(trades["ev_id"].nunique())
    per_ev_month = paid / n_ev / months if n_ev else 0.0
    threshold = float(pts.get("min_expected_reward_krw_per_month", 0))
    by_source = {"요금 차익": float(trades["tou_krw"].sum()), "피크 저감": float(trades["peak_krw"].sum()),
                 "출력제어 완화": float(trades["curtail_krw"].sum())}
    return {
        "vehicles": n_ev,
        "months": round(months, 2),
        "benefit_krw": round(total_value, 0),
        "benefit_by_source_krw": {k: round(v, 0) for k, v in by_source.items()},
        "participant_krw": round(paid, 0),
        "operator_krw": round(total_value - paid, 0),
        "payout_ratio_pct": round(paid / total_value * 100, 1) if total_value else None,
        "per_vehicle_month_krw": round(per_ev_month, 0),
        "min_expected_reward_krw_per_month": threshold,
        "incentive_sufficient": per_ev_month >= threshold if threshold else None,
        "judgement": ("참여 유인 충분" if per_ev_month >= threshold else "참여 유인 부족 — 배분율·재원 확대 검토") if threshold else "기준 미설정",
        "label": "모의(추론 실적·가정 단가)",
    }


# ------------------------------------------------------------------ 4) 정산 회차
def _period_filter(trades: pd.DataFrame, period_from: str, period_to: str) -> pd.DataFrame:
    return trades[(trades["trade_date"] >= period_from) & (trades["trade_date"] <= period_to)].reset_index(drop=True)


def create_run(trades: pd.DataFrame, period_from: str, period_to: str, params: dict, actor: str,
               generation: str = "inferred", data_source: str = "synthetic", con: sqlite3.Connection | None = None) -> dict:
    """기간의 실적을 산정 회차(draft)로 저장한다 — 포인트는 아직 원장에 적립하지 않는다.
    이미 다른 회차(산정·확정)에 들어간 차량·일은 건너뛴다."""
    if period_from > period_to:
        raise SettlementError("기간 시작이 끝보다 늦습니다")
    part = _period_filter(trades, period_from, period_to)
    own = con is None
    con = con or store.get_connection()
    try:
        now = _now()
        cur = con.execute("INSERT INTO settlement_runs (period_from, period_to, status, created_at, created_by) VALUES (?,?,?,?,?)",
                          (period_from, period_to, "draft", now, actor))
        run_id = cur.lastrowid
        inserted = skipped = 0
        for r in part.to_dict("records"):
            c = con.execute(
                "INSERT OR IGNORE INTO virtual_trades (ev_id, trade_date, affiliation, peak_discharge_kwh, absorb_kwh, value_krw, points, "
                "generation, data_source, settled_at, run_id, tou_krw, peak_krw, curtail_krw, consent_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["ev_id"], r["trade_date"], r["affiliation"], r["peak_discharge_kwh"], r["absorb_kwh"], r["value_krw"], r["points"],
                 generation, data_source, now, run_id, r["tou_krw"], r["peak_krw"], r["curtail_krw"], r["consent_note"]))
            if c.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        if inserted == 0:
            raise SettlementError(f"새로 정산할 실적이 없습니다(기간 내 {skipped}건은 이미 다른 회차에서 정산됨)")
        stored = pd.read_sql_query("SELECT * FROM virtual_trades WHERE run_id=?", con, params=(run_id,))
        totals = {"trades": inserted, "skipped_already_settled": skipped,
                  "points": int(stored["points"].sum()) if not stored.empty else 0,
                  "economics": economics_check(stored.assign(trade_date=stored["trade_date"]) if not stored.empty else stored, params)}
        con.execute("UPDATE settlement_runs SET totals=? WHERE run_id=?", (json.dumps(totals, ensure_ascii=False), run_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        if own:
            con.close()
    return get_run(run_id)


def confirm_run(run_id: int, actor: str, note: str | None = None) -> dict:
    """산정 회차를 확정하고 거래마다 포인트 적립(earn) 한 줄을 원장에 남긴다."""
    con = store.get_connection()
    try:
        r = con.execute("SELECT status FROM settlement_runs WHERE run_id=?", (run_id,)).fetchone()
        if not r:
            raise SettlementError("정산 회차를 찾을 수 없습니다")
        if r[0] != "draft":
            raise SettlementError(f"산정 상태의 회차만 확정할 수 있습니다(현재 {RUN_STATUS_LABELS[r[0]]})")
        if not con.execute("SELECT 1 FROM virtual_trades WHERE run_id=? LIMIT 1", (run_id,)).fetchone():
            raise SettlementError("거래가 없는 회차는 확정할 수 없습니다 — 취소하세요")
        now = _now()
        rows = con.execute("SELECT trade_id, ev_id, trade_date, points FROM virtual_trades WHERE run_id=?", (run_id,)).fetchall()
        con.executemany(
            "INSERT INTO point_ledger (ev_id, kind, points, trade_id, memo, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
            [(ev, "earn", pts, tid, f"{day} 가상 전력 거래 정산(회차 {run_id})", now, actor) for tid, ev, day, pts in rows])
        con.execute("UPDATE settlement_runs SET status='confirmed', decided_at=?, decided_by=?, note=? WHERE run_id=?",
                    (now, actor, (note or "").strip()[:200] or None, run_id))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return get_run(run_id)


def cancel_run(run_id: int, actor: str, note: str) -> dict:
    """산정 회차를 취소한다 — 그 회차의 거래를 지워 같은 기간을 다시 산정할 수 있게 한다(확정된 회차는 취소 불가)."""
    note = (note or "").strip()
    if len(note) < 2:
        raise SettlementError("취소 사유를 입력하세요")
    con = store.get_connection()
    try:
        r = con.execute("SELECT status FROM settlement_runs WHERE run_id=?", (run_id,)).fetchone()
        if not r:
            raise SettlementError("정산 회차를 찾을 수 없습니다")
        if r[0] != "draft":
            raise SettlementError("확정된 회차는 취소할 수 없습니다 — 포인트 조정으로 처리하세요")
        con.execute("DELETE FROM virtual_trades WHERE run_id=?", (run_id,))
        con.execute("UPDATE settlement_runs SET status='cancelled', decided_at=?, decided_by=?, note=? WHERE run_id=?",
                    (_now(), actor, note[:200], run_id))
        con.commit()
    finally:
        con.close()
    return get_run(run_id)


def get_run(run_id: int) -> dict:
    con = store.get_connection()
    try:
        r = con.execute("SELECT run_id, period_from, period_to, status, totals, created_at, created_by, decided_at, decided_by, note "
                        "FROM settlement_runs WHERE run_id=?", (run_id,)).fetchone()
        if not r:
            raise SettlementError("정산 회차를 찾을 수 없습니다")
        out = dict(zip(("run_id", "period_from", "period_to", "status", "totals", "created_at", "created_by", "decided_at",
                        "decided_by", "note"), r))
        out["totals"] = json.loads(out["totals"]) if out["totals"] else None
        out["status_label"] = RUN_STATUS_LABELS[out["status"]]
        return out
    finally:
        con.close()


def list_runs(limit: int = 50) -> list[dict]:
    con = store.get_connection()
    try:
        ids = [r[0] for r in con.execute("SELECT run_id FROM settlement_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()]
    finally:
        con.close()
    return [get_run(i) for i in ids]


def run_trades(run_id: int, limit: int = 500) -> list[dict]:
    con = store.get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT ev_id, trade_date, affiliation, peak_discharge_kwh, absorb_kwh, tou_krw, peak_krw, curtail_krw, value_krw, points, "
            "consent_note FROM virtual_trades WHERE run_id=? ORDER BY trade_date, ev_id LIMIT ?", con, params=(run_id, limit))
    finally:
        con.close()
    return df.to_dict("records")


# 호환: 한 번에 산정+확정(전체 기간) — 테스트·간편 실행용
def settle(trades: pd.DataFrame, generation: str, data_source: str, params: dict | None = None, actor: str = "settlement") -> dict:
    if generation not in ("measured", "inferred"):
        raise ValueError("generation은 measured 또는 inferred")
    if trades.empty:
        return {"inserted": 0, "skipped_already_settled": 0, "points_issued": 0, "run_id": None}
    params = params or {"points": {"krw_per_point": 1, "min_expected_reward_krw_per_month": 0}}
    try:
        run = create_run(trades, trades["trade_date"].min(), trades["trade_date"].max(), params, actor, generation, data_source)
    except SettlementError:   # 기간 전체가 이미 정산됨 — 새로 적립할 것이 없다
        return {"inserted": 0, "skipped_already_settled": len(trades), "points_issued": 0, "run_id": None}
    confirmed = confirm_run(run["run_id"], actor)
    t = confirmed["totals"]
    return {"inserted": t["trades"], "skipped_already_settled": t["skipped_already_settled"], "points_issued": t["points"],
            "run_id": run["run_id"]}


# ------------------------------------------------------------------ 5) 조회·조정·문의
def balance(ev_id: str, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or store.get_connection()
    try:
        return int(con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0])
    finally:
        if own:
            con.close()


def adjust(ev_id: str, points: int, reason: str, actor: str, inquiry_id: int | None = None) -> dict:
    """관리자 포인트 조정(가산·차감) — 사유를 원장에 남긴다."""
    reason = " ".join((reason or "").split())[:200]
    if not reason:
        raise SettlementError("조정 사유를 입력하세요")
    if points == 0:
        raise SettlementError("조정 포인트는 0이 아니어야 합니다")
    con = store.get_connection()
    try:
        con.execute("INSERT INTO point_ledger (ev_id, kind, points, trade_id, memo, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
                    (ev_id, "adjust", int(points), None, f"관리자 조정: {reason}" + (f" (문의 {inquiry_id})" if inquiry_id else ""),
                     _now(), actor))
        con.commit()
        return {"ev_id": ev_id, "points": int(points), "balance_points": balance(ev_id, con)}
    finally:
        con.close()


def create_inquiry(ev_id: str, message: str, trade_date: str | None = None) -> dict:
    message = " ".join((message or "").split())[:500]
    if len(message) < 2:
        raise SettlementError("문의 내용을 입력하세요")
    con = store.get_connection()
    try:
        open_n = con.execute("SELECT COUNT(*) FROM settlement_inquiries WHERE ev_id=? AND status='open'", (ev_id,)).fetchone()[0]
        if open_n >= 5:
            raise SettlementError("처리 중인 문의가 많습니다. 답변 후 다시 남겨 주세요")
        cur = con.execute("INSERT INTO settlement_inquiries (ev_id, trade_date, message, created_at) VALUES (?,?,?,?)",
                          (ev_id, trade_date, message, _now()))
        con.commit()
        return {"inquiry_id": cur.lastrowid, "status": "open"}
    finally:
        con.close()


_INQ_COLS = ("inquiry_id", "ev_id", "trade_date", "message", "status", "created_at", "resolved_at", "resolved_by", "answer", "adjust_points")


def list_inquiries(status: str | None = None, ev_id: str | None = None) -> list[dict]:
    q, args = f"SELECT {', '.join(_INQ_COLS)} FROM settlement_inquiries WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if ev_id:
        q += " AND ev_id=?"; args.append(ev_id)
    con = store.get_connection()
    try:
        return [dict(zip(_INQ_COLS, r)) for r in con.execute(q + " ORDER BY inquiry_id DESC LIMIT 200", args).fetchall()]
    finally:
        con.close()


def resolve_inquiry(inquiry_id: int, answer: str, actor: str, adjust_points: int = 0) -> dict:
    answer = " ".join((answer or "").split())[:500]
    if len(answer) < 2:
        raise SettlementError("답변을 입력하세요")
    con = store.get_connection()
    try:
        r = con.execute("SELECT ev_id, status FROM settlement_inquiries WHERE inquiry_id=?", (inquiry_id,)).fetchone()
        if not r:
            raise SettlementError("문의를 찾을 수 없습니다")
        if r[1] != "open":
            raise SettlementError("이미 답변한 문의입니다")
        con.execute("UPDATE settlement_inquiries SET status='resolved', resolved_at=?, resolved_by=?, answer=?, adjust_points=? WHERE inquiry_id=?",
                    (_now(), actor, answer, int(adjust_points) or None, inquiry_id))
        con.commit()
        ev_id = r[0]
    finally:
        con.close()
    if adjust_points:
        adjust(ev_id, int(adjust_points), answer, actor, inquiry_id)
    return {"inquiry_id": inquiry_id, "status": "resolved", "adjust_points": int(adjust_points)}


def owner_statement(ev_id: str, limit: int = 30) -> dict:
    """참여자 본인 — 확정 포인트 잔액, 확정 대기(산정 중) 포인트, 재원별 내역, 조정·문의."""
    con = store.get_connection()
    try:
        rows = con.execute(
            "SELECT t.trade_date, t.peak_discharge_kwh, t.absorb_kwh, t.tou_krw, t.peak_krw, t.curtail_krw, t.points, t.generation, "
            "COALESCE(r.status, 'confirmed'), t.consent_note FROM virtual_trades t LEFT JOIN settlement_runs r ON r.run_id = t.run_id "
            "WHERE t.ev_id=? ORDER BY t.trade_date DESC LIMIT ?", (ev_id, limit)).fetchall()
        agg = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(t.peak_discharge_kwh),0), COALESCE(SUM(t.absorb_kwh),0), "
            "COALESCE(SUM(CASE WHEN r.status='draft' THEN t.points ELSE 0 END),0) "
            "FROM virtual_trades t LEFT JOIN settlement_runs r ON r.run_id = t.run_id WHERE t.ev_id=?", (ev_id,)).fetchone()
        adjustments = con.execute("SELECT points, memo, created_at FROM point_ledger WHERE ev_id=? AND kind='adjust' ORDER BY entry_id DESC LIMIT 10",
                                  (ev_id,)).fetchall()
        bal = int(con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0])
    finally:
        con.close()
    return {
        "ev_id": ev_id,
        "balance_points": bal,
        "pending_points": int(agg[3]),
        "trade_days": int(agg[0]),
        "total_peak_discharge_kwh": round(float(agg[1]), 2),
        "total_absorb_kwh": round(float(agg[2]), 2),
        "history": [
            {"date": r[0], "peak_discharge_kwh": r[1], "absorb_kwh": r[2],
             "by_source_krw": {"요금 차익": r[3], "피크 저감": r[4], "출력제어 완화": r[5]},
             "points": r[6], "generation": r[7], "generation_label": "실측" if r[7] == "measured" else "추론(모의)",
             "status": r[8], "status_label": RUN_STATUS_LABELS.get(r[8], r[8]), "consent_note": r[9]}
            for r in rows
        ],
        "adjustments": [{"points": a[0], "memo": a[1], "at": a[2]} for a in adjustments],
        "inquiries": list_inquiries(ev_id=ev_id)[:5],
        "note": "모의 포인트입니다(실제 금전 지급 아님). 확정된 정산만 잔액에 반영되고, 1차년도 방전량은 최적 스케줄 기반 추론값입니다.",
    }


def summary(params: dict | None = None) -> dict:
    """관리자 집계 — 관용/민간별 참여·kWh·포인트(확정 기준), 정산 가능범위(Y2E04), 경제성 검증."""
    con = store.get_connection()
    try:
        df = pd.read_sql_query(
            "SELECT t.*, COALESCE(r.status, 'confirmed') AS run_status FROM virtual_trades t "
            "LEFT JOIN settlement_runs r ON r.run_id = t.run_id", con)
        open_inq = con.execute("SELECT COUNT(*) FROM settlement_inquiries WHERE status='open'").fetchone()[0]
    finally:
        con.close()
    conf = df[df["run_status"] == "confirmed"] if not df.empty else df
    groups = []
    for aff, label in AFFILIATION_LABELS.items():
        g = conf[conf["affiliation"] == aff] if not conf.empty else conf
        groups.append({"affiliation": aff, "label": label, "vehicles": int(g["ev_id"].nunique()) if not g.empty else 0,
                       "trade_days": int(len(g)), "peak_discharge_kwh": round(float(g["peak_discharge_kwh"].sum()), 1) if not g.empty else 0.0,
                       "absorb_kwh": round(float(g["absorb_kwh"].sum()), 1) if not g.empty else 0.0,
                       "value_krw": round(float(g["value_krw"].sum()), 0) if not g.empty else 0.0,
                       "points": int(g["points"].sum()) if not g.empty else 0})
    per_day = conf.groupby(["ev_id", "trade_date"])["points"].sum() if not conf.empty else pd.Series(dtype=float)
    per_vehicle = []
    if not conf.empty:
        pv = conf.groupby(["ev_id", "affiliation"]).agg(trade_days=("points", "size"), peak=("peak_discharge_kwh", "sum"),
                                                        absorb=("absorb_kwh", "sum"), points=("points", "sum")).reset_index()
        per_vehicle = [{"ev_id": r.ev_id, "affiliation": r.affiliation, "label": AFFILIATION_LABELS.get(r.affiliation, r.affiliation),
                        "trade_days": int(r.trade_days), "peak_discharge_kwh": round(float(r.peak), 2),
                        "absorb_kwh": round(float(r.absorb), 2), "points": int(r.points)}
                       for r in pv.sort_values("points", ascending=False).itertuples()]
    econ = economics_check(conf[TRADE_COLS[:2] + ["tou_krw", "peak_krw", "curtail_krw", "value_krw", "points"]], params) \
        if params and not conf.empty else None
    return {
        "groups": groups,
        "total_points": int(conf["points"].sum()) if not conf.empty else 0,
        "pending_points": int(df.loc[df["run_status"] == "draft", "points"].sum()) if not df.empty else 0,
        "period": {"from": conf["trade_date"].min() if not conf.empty else None, "to": conf["trade_date"].max() if not conf.empty else None},
        "by_generation": conf["generation"].value_counts().to_dict() if not conf.empty else {},
        "settlement_range": {"per_vehicle_day_points": {
            "min": int(per_day.min()) if len(per_day) else 0, "p50": int(per_day.median()) if len(per_day) else 0,
            "p90": int(per_day.quantile(0.9)) if len(per_day) else 0, "max": int(per_day.max()) if len(per_day) else 0}},
        "economics": econ,
        "open_inquiries": int(open_inq),
        "per_vehicle": per_vehicle,
    }


def simulate_incentives(trades: pd.DataFrame, params: dict, discount_rate_pct: float = 10.0,
                        revenue_share_pct: float | None = None) -> dict:
    """인센티브 유형별 시뮬레이션(Y2D03·S02 기초) — 같은 실적에 대해
    (a) 포인트 적립, (b) 충전요금 할인, (c) 수익배분, (d) 충전 마일리지(흡수·방전 kWh당 적립)를 비교한다."""
    pts = params["points"]
    share = pts["owner_share_pct"] if revenue_share_pct is None else revenue_share_pct
    if trades.empty:
        return {"vehicles": 0, "benefit_krw": 0, "options": [], "label": "모의(가정값)"}
    value = float(trades["value_krw"].sum())
    saving = float(trades["saving_krw"].sum()) if "saving_krw" in trades.columns else value
    kwh = float(trades["absorb_kwh"].sum() + trades["peak_discharge_kwh"].sum())
    n_ev = int(trades["ev_id"].nunique())
    options = [
        {"type": "points", "label": "모의 포인트 적립(재원 배분)", "participant_krw": round(float(trades["points"].sum()) * pts["krw_per_point"], 0)},
        {"type": "charge_discount", "label": f"충전요금 할인({discount_rate_pct:.0f}% 상당)", "participant_krw": round(saving * discount_rate_pct / 100, 0)},
        {"type": "revenue_share", "label": f"수익배분({share:.0f}%)", "participant_krw": round(value * share / 100, 0)},
        {"type": "mileage", "label": f"충전 마일리지({pts.get('mileage_points_per_kwh', 0)}P/kWh)",
         "participant_krw": round(kwh * pts.get("mileage_points_per_kwh", 0) * pts["krw_per_point"], 0)},
    ]
    for o in options:
        o["per_vehicle_krw"] = round(o["participant_krw"] / n_ev, 0) if n_ev else 0
        o["benefit_ratio_pct"] = round(o["participant_krw"] / value * 100, 1) if value else None
    return {"vehicles": n_ev, "benefit_krw": round(value, 0), "options": options, "label": "모의(가정값)",
            "note": "benefit_ratio_pct = 참여자 몫 ÷ 재원 배분 편익. 100%를 넘으면 재원보다 많이 지급하는 구조라 지속 불가."}
