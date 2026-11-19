"""서포터즈 미션 — 실증에 도움이 되는 참여 행동을 미션으로 제시하고, 완료하면 모의 포인트로 보상한다(가상 리워드).

계획서 근거
- 참여자가 체감할 수 있는 이익공유 구조(p.30~31), 시민 체감형 가상 포인트(p.48)
- 1차년도 서포터즈 참여 결과를 인센티브 모델 설계 근거로 활용(p.33), 방전 수용도·보상 기대 수준 VoC(p.34)

미션 진행도는 실제 기록에서 계산한다(동의 이력·설문 응답·충전 추천 응답·확정된 정산 실적·주행거리 입력).
보상은 기간(일회·주간·월간)마다 한 번만 받을 수 있고, 받으면 포인트 원장에 적립된다. 실제 금전 지급은 없다.
관리자는 미션별 사용 여부와 보상 포인트를 바꿀 수 있다(mission_settings).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pandas as pd

from src.pipeline import store

# (id, 제목, 설명, 주기, 목표, 단위, 기본 보상 포인트)
MISSIONS = [
    ("consent_location", "위치정보 이용 동의", "충전 위치 기반 분석(출력제어·피크 시나리오)에 참여해요.", "once", 1, "회", 100),
    ("follow_recommendation", "추천 충전 시간 따르기", "이번 주 충전 안내 중 '추천대로 할게요'를 2번 이상 선택해요.", "weekly", 2, "회", 150),
    ("peak_discharge", "피크 시간대 방전 기여", "이번 달 피크 시간대(저녁) 방전 실적을 10kWh 이상 쌓아요(확정 정산 기준).", "monthly", 10, "kWh", 300),
    ("surplus_absorb", "재생에너지 흡수 충전", "이번 달 과잉발전 시간(낮) 흡수 충전 실적을 20kWh 이상 쌓아요(확정 정산 기준).", "monthly", 20, "kWh", 300),
    ("survey_answer", "실증 의견 남기기", "이번 달 진행 중인 설문에 응답해요.", "monthly", 1, "회", 200),
    ("odometer_input", "누적 주행거리 입력", "이번 달 누적 주행거리를 한 번 이상 입력해요.", "monthly", 1, "회", 50),
]
PERIOD_LABELS = {"once": "1회", "weekly": "매주", "monthly": "매월"}
LEVELS = [(0, "새싹"), (1_000, "브론즈"), (5_000, "실버"), (20_000, "골드"), (50_000, "플래티넘")]


class MissionError(ValueError):
    pass


def _kst_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)


def period_key(period: str, now: pd.Timestamp | None = None) -> str:
    now = now or _kst_now()
    if period == "once":
        return "once"
    if period == "weekly":
        y, w, _ = now.isocalendar()
        return f"{y}-W{w:02d}"
    return now.strftime("%Y-%m")


def _period_range(period: str, now: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    if period == "weekly":
        start = (now - pd.Timedelta(days=now.dayofweek)).normalize()
        return start, start + pd.Timedelta(days=7)
    start = now.normalize().replace(day=1)
    return start, start + pd.offsets.MonthBegin(1)


def level_of(points: int) -> dict:
    name, nxt = LEVELS[0][1], None
    for i, (thr, lbl) in enumerate(LEVELS):
        if points >= thr:
            name = lbl
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    return {"name": name, "points": int(points), "next_name": nxt[1] if nxt else None,
            "next_points": nxt[0] if nxt else None, "to_next": max(nxt[0] - int(points), 0) if nxt else 0}


def settings(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or store.get_connection()
    try:
        rows = con.execute("SELECT mission_id, enabled, points FROM mission_settings").fetchall()
    finally:
        if own:
            con.close()
    return {r[0]: {"enabled": bool(r[1]), "points": int(r[2])} for r in rows}


def definitions(con: sqlite3.Connection | None = None) -> list[dict]:
    st = settings(con)
    out = []
    for mid, title, desc, period, target, unit, pts in MISSIONS:
        s = st.get(mid, {})
        out.append({"id": mid, "title": title, "description": desc, "period": period, "period_label": PERIOD_LABELS[period],
                    "target": target, "unit": unit, "points": s.get("points", pts), "default_points": pts,
                    "enabled": s.get("enabled", True)})
    return out


def update_setting(mission_id: str, enabled: bool, points: int, actor: str) -> dict:
    if mission_id not in {m[0] for m in MISSIONS}:
        raise MissionError("없는 미션입니다")
    if not 0 <= points <= 100_000:
        raise MissionError("보상 포인트는 0~100,000 사이여야 합니다")
    con = store.get_connection()
    try:
        con.execute("INSERT INTO mission_settings (mission_id, enabled, points, updated_by, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(mission_id) DO UPDATE SET enabled=excluded.enabled, points=excluded.points, "
                    "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (mission_id, int(enabled), int(points), actor, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
        return next(d for d in definitions(con) if d["id"] == mission_id)
    finally:
        con.close()


def _progress(mid: str, period: str, ev_id: str, username: str | None, con: sqlite3.Connection, now: pd.Timestamp) -> float:
    from src.privacy import consent
    if mid == "consent_location":
        return 1.0 if consent.has_consent(ev_id, "location") else 0.0
    start, end = _period_range(period, now)
    s_iso, e_iso = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    if mid == "follow_recommendation":
        # 응답 시각은 UTC로 저장된다 — 날짜 범위 비교라 하루 미만 오차는 허용
        r = con.execute("SELECT COUNT(*) FROM advisories WHERE ev_id=? AND status='accepted' AND substr(responded_at,1,10) >= ? "
                        "AND substr(responded_at,1,10) < ?", (ev_id, s_iso, e_iso)).fetchone()
        return float(r[0])
    if mid in ("peak_discharge", "surplus_absorb"):
        col = "peak_discharge_kwh" if mid == "peak_discharge" else "absorb_kwh"
        r = con.execute(f"SELECT COALESCE(SUM(t.{col}),0) FROM virtual_trades t LEFT JOIN settlement_runs r ON r.run_id=t.run_id "
                        "WHERE t.ev_id=? AND COALESCE(r.status,'confirmed')='confirmed' AND t.trade_date >= ? AND t.trade_date < ?",
                        (ev_id, s_iso, e_iso)).fetchone()
        return round(float(r[0]), 2)
    if mid == "survey_answer":
        pcon = consent.connect()
        try:
            try:
                r = pcon.execute("SELECT COUNT(*) FROM survey_responses WHERE ev_id=? AND substr(created_at,1,10) >= ? "
                                 "AND substr(created_at,1,10) < ?", (ev_id, s_iso, e_iso)).fetchone()
            except sqlite3.OperationalError:   # 설문 기능을 한 번도 안 쓴 DB
                return 0.0
        finally:
            pcon.close()
        return float(r[0])
    if mid == "odometer_input":
        if not username:
            return 0.0
        r = con.execute("SELECT COUNT(*) FROM odometer_logs WHERE username=? AND substr(recorded_at,1,10) >= ? "
                        "AND substr(recorded_at,1,10) < ?", (username, s_iso, e_iso)).fetchone()
        return float(r[0])
    return 0.0


def status_for(ev_id: str, username: str | None = None, now: pd.Timestamp | None = None) -> list[dict]:
    """차주 미션 목록 — 진행도·완료 여부·이번 기간 보상 수령 여부."""
    now = now or _kst_now()
    con = store.get_connection()
    try:
        claimed = {(r[0], r[1]) for r in con.execute(
            "SELECT mission_id, period_key FROM mission_completions WHERE ev_id=?", (ev_id,)).fetchall()}
        out = []
        for d in definitions(con):
            if not d["enabled"]:
                continue
            key = period_key(d["period"], now)
            prog = _progress(d["id"], d["period"], ev_id, username, con, now)
            out.append({**d, "period_key": key, "progress": prog, "progress_pct": round(min(prog / d["target"], 1.0) * 100),
                        "completed": prog >= d["target"], "claimed": (d["id"], key) in claimed})
        return out
    finally:
        con.close()


def claim(ev_id: str, mission_id: str, username: str | None = None, now: pd.Timestamp | None = None) -> dict:
    """완료한 미션 보상을 받는다 — 같은 기간에 한 번만, 포인트 원장에 적립."""
    now = now or _kst_now()
    m = next((x for x in status_for(ev_id, username, now) if x["id"] == mission_id), None)
    if m is None:
        raise MissionError("진행 중인 미션이 아닙니다")
    if not m["completed"]:
        raise MissionError(f"아직 완료하지 않았어요({m['progress']}/{m['target']}{m['unit']})")
    if m["claimed"]:
        raise MissionError("이번 기간 보상은 이미 받았어요")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = store.get_connection()
    try:
        try:
            con.execute("INSERT INTO mission_completions (ev_id, mission_id, period_key, points, completed_at) VALUES (?,?,?,?,?)",
                        (ev_id, mission_id, m["period_key"], m["points"], ts))
        except sqlite3.IntegrityError as exc:
            raise MissionError("이번 기간 보상은 이미 받았어요") from exc
        con.execute("INSERT INTO point_ledger (ev_id, kind, points, trade_id, memo, created_at, created_by) VALUES (?,?,?,?,?,?,?)",
                    (ev_id, "earn", m["points"], None, f"미션 보상: {m['title']} ({m['period_key']})", ts, "mission"))
        con.commit()
        bal = int(con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0])
    finally:
        con.close()
    return {"mission_id": mission_id, "points": m["points"], "period_key": m["period_key"], "balance_points": bal, "level": level_of(bal)}


def admin_overview() -> dict:
    """관리자 — 미션 설정과 기간별 완료(보상) 현황."""
    con = store.get_connection()
    try:
        defs = definitions(con)
        stats = con.execute("SELECT mission_id, COUNT(*), COUNT(DISTINCT ev_id), COALESCE(SUM(points),0) FROM mission_completions "
                            "GROUP BY mission_id").fetchall()
        recent = con.execute("SELECT ev_id, mission_id, period_key, points, completed_at FROM mission_completions "
                             "ORDER BY completion_id DESC LIMIT 30").fetchall()
    finally:
        con.close()
    st = {r[0]: {"completions": r[1], "vehicles": r[2], "points_paid": int(r[3])} for r in stats}
    return {"missions": [{**d, **st.get(d["id"], {"completions": 0, "vehicles": 0, "points_paid": 0})} for d in defs],
            "recent": [dict(zip(("ev_id", "mission_id", "period_key", "points", "completed_at"), r)) for r in recent],
            "levels": [{"name": n, "from_points": t} for t, n in LEVELS],
            "note": "미션 보상은 모의 포인트이며 실제 금전 지급은 없습니다(계획서 p.48)."}
