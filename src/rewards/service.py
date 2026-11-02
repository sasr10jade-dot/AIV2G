"""미션·시범 포인트 — 달성 판정과 적립 원장.

판정은 서버가 기존 기록(알림 동의, 충전 안내 감사로그)에서 계산한다. 클라이언트가 "달성했다"고 알리는 방식은 없다.
포인트는 `incentive` 동의가 있을 때만 적립하고(철회하면 이후 적립 중단, 이미 쌓은 포인트는 유지), 같은 미션은
같은 기간(1회성은 평생, 주간은 ISO 주)에 한 번만 적립한다(UNIQUE). 월 적립은 `config/incentive_policy.json`의
monthly_cap_points를 넘지 않는다. 모든 수치는 협의 전 가정값(verified=false)이며 실금 정산이 아니다.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.experiment.incentive import IncentivePolicy
from src.paths import PROJECT_ROOT
from src.privacy import audit, consent
from src.timeutil import kst_now

MISSIONS_CONFIG = PROJECT_ROOT / "config" / "missions.json"
INCENTIVE_CONSENT_VERSION = "incentive-v1"
NOTE = "시범 포인트예요. 실제 금전 정산이 아니며, 보상 수치는 협의 전 예시값이에요."


def load_config(path: Path | None = None) -> dict:
    return json.loads(Path(path or MISSIONS_CONFIG).read_text(encoding="utf-8"))


def _connect() -> sqlite3.Connection:
    con = consent.connect()   # 같은 privacy.db — 탈퇴·파기 정책을 함께 적용하기 쉽다
    con.execute(
        """CREATE TABLE IF NOT EXISTS point_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ev_id TEXT NOT NULL,
            mission_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            points INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (ev_id, mission_id, period_key)
        )"""
    )
    con.commit()
    return con


def _plans_table(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS guide_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ev_id TEXT NOT NULL,
            issued_kst TEXT NOT NULL,
            departure TEXT,
            intervals TEXT NOT NULL,
            response TEXT,
            responded_at TEXT
        )""")
    cols = {r[1] for r in con.execute("PRAGMA table_info(guide_plans)")}
    for col in ("response", "responded_at"):   # 이전에 만든 표에는 칸을 더해 준다
        if col not in cols:
            con.execute(f"ALTER TABLE guide_plans ADD COLUMN {col} TEXT")


def record_plan(ev_id: str, guide: dict, issued_kst: datetime | None = None) -> int | None:
    """발급한 충전 안내의 추천 충전 시간대를 저장하고 기록 번호를 돌려준다(추천 시간대가 없으면 None).
    실제 충전 기록과 대조(미션 확인)하는 근거이자, 이용자의 '추천대로 할게요/나중에' 응답을 붙이는 대상이다."""
    intervals = [{"start": i["start"], "end": i["end"], "kwh": i["kwh"]} for i in (guide.get("plan") or {}).get("charge", [])]
    if not intervals:
        return None
    issued = (issued_kst or kst_now())
    con = _connect()
    try:
        _plans_table(con)
        con.execute("INSERT INTO guide_plans (ev_id, issued_kst, departure, intervals) VALUES (?,?,?,?)",
                    (ev_id, pd.Timestamp(issued).isoformat(), (guide.get("period") or {}).get("departure"), json.dumps(intervals)))
        con.commit()
        return con.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        con.close()


RESPONSES = ("accepted", "declined")


def respond_plan(ev_id: str, plan_id: int, status: str) -> bool:
    """내 추천에 대한 응답(accepted=추천대로 할게요, declined=나중에)을 남긴다. 다른 차량의 기록이거나 없으면 False."""
    if status not in RESPONSES:
        raise ValueError("응답은 accepted 또는 declined 이어야 합니다")
    con = _connect()
    try:
        _plans_table(con)
        cur = con.execute("UPDATE guide_plans SET response=?, responded_at=? WHERE id=? AND ev_id=?",
                          (status, datetime.now(timezone.utc).isoformat(), plan_id, ev_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


_telemetry_cache: dict = {"at": 0.0, "df": None}
TELEMETRY_TTL_S = 600


def _hourly_charging(ev_id: str) -> pd.DataFrame | None:
    """차량의 시간별 충전량(kWh) 기록 — OBD 텔레메트리. 모바일 가입 차량(EV-U…)은 아직 OBD가 없어 None."""
    from src.registry.service import AUTO_EV_PREFIX
    if ev_id.startswith(AUTO_EV_PREFIX):
        return None
    if _telemetry_cache["df"] is None or time.monotonic() - _telemetry_cache["at"] > TELEMETRY_TTL_S:
        from src.datagen.config import SimConfig
        from src.pipeline.source import load_scenario
        ev_df, _c, _g, _real = load_scenario(SimConfig())
        _telemetry_cache.update(at=time.monotonic(), df=ev_df[["timestamp", "ev_id", "charging_kw"]].copy())
    df = _telemetry_cache["df"]
    return df[df["ev_id"] == ev_id]


def check_follow(plan: dict, hourly: pd.DataFrame | None, ver: dict) -> str:
    """추천대로 충전했는지 — 'ok' | 'no' | 'pending'(충전 기록이 아직 그 시각까지 없음) | 'unavailable'(기록 없는 차량)."""
    if hourly is None or hourly.empty:
        return "unavailable"
    tol = pd.Timedelta(hours=ver["tolerance_hours"])
    wins = [(pd.Timestamp(i["start"]) - tol, pd.Timestamp(i["end"]) + tol) for i in plan["intervals"]]
    end = max(w[1] for w in wins)
    if plan.get("departure"):
        end = max(end, pd.Timestamp(plan["departure"]) + tol)
    if hourly["timestamp"].max() < end:
        return "pending"
    start = pd.Timestamp(plan["issued_kst"]).floor("h")
    span = hourly[(hourly["timestamp"] >= start) & (hourly["timestamp"] < end)]
    total = float(span["charging_kw"].sum())   # 시간별 평균 kW × 1시간 = kWh
    recommended = sum(i["kwh"] for i in plan["intervals"])
    if total <= 0 or total < recommended * ver["min_charged_ratio"]:
        return "no"
    in_win = sum(float(span[(span["timestamp"] >= a) & (span["timestamp"] < b)]["charging_kw"].sum()) for a, b in wins)
    return "ok" if min(in_win, total) / total >= ver["min_follow_share"] else "no"


def _follow_status(con: sqlite3.Connection, ev_id: str, now_kst: datetime, ver: dict) -> tuple[int, str]:
    """이번 주에 확인된 날짜 수와 확인 상태('unavailable'|'pending'|'tracking')."""
    _plans_table(con)
    week_start = pd.Timestamp(now_kst).normalize() - pd.Timedelta(days=pd.Timestamp(now_kst).weekday())
    rows = con.execute("SELECT issued_kst, departure, intervals FROM guide_plans WHERE ev_id=?", (ev_id,)).fetchall()
    hourly = _hourly_charging(ev_id)
    if hourly is None or hourly.empty:
        return 0, "unavailable"
    ok_days, pending = set(), False
    for issued, departure, intervals in rows:
        if pd.Timestamp(issued) < week_start:
            continue
        plan = {"issued_kst": issued, "departure": departure, "intervals": json.loads(intervals)}
        res = check_follow(plan, hourly, ver)
        if res == "ok":
            ok_days.add(pd.Timestamp(departure or issued).date())   # 같은 날 안내를 여러 번 받아도 하루 한 번만 센다
        elif res == "pending":
            pending = True
    return len(ok_days), ("pending" if pending else "tracking")


def _kst_to_utc(naive_kst: datetime) -> datetime:
    return (naive_kst - timedelta(hours=9)).replace(tzinfo=timezone.utc)


def _week_start_utc(now_kst: datetime) -> datetime:
    monday = (now_kst - timedelta(days=now_kst.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return _kst_to_utc(monday)


def _period_key(period: str, now_kst: datetime) -> str:
    if period == "once":
        return "once"
    iso = now_kst.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _guide_rows(username: str) -> list[tuple[datetime, bool]]:
    """이용자의 충전 안내 발급 기록 — (시각, 안전 검증 통과 여부)."""
    con = audit._connect()
    try:
        rows = con.execute("SELECT ts, detail FROM audit_log WHERE actor=? AND action='owner_guide_issue'", (username,)).fetchall()
    finally:
        con.close()
    out = []
    for ts, detail in rows:
        d = json.loads(detail) if detail else {}
        out.append((datetime.fromisoformat(ts), bool(d.get("safety_passed"))))
    return out


def _progress(metric: str, period: str, ev_id: str, username: str, now_kst: datetime, guide_rows: list | None = None) -> int:
    if metric == "notify_consent":
        return int(consent.has_consent(ev_id, "notify"))
    rows = guide_rows if guide_rows is not None else _guide_rows(username)   # 감사로그를 미션마다 다시 읽지 않도록 한 번 읽은 값을 받는다
    if period == "weekly":
        start = _week_start_utc(now_kst)
        rows = [r for r in rows if r[0] >= start]
    if metric == "safe_guide_views":
        return sum(1 for _, ok in rows if ok)
    return len(rows)   # guide_views


def _month_earned(con: sqlite3.Connection, ev_id: str, now_kst: datetime) -> int:
    month_start = _kst_to_utc(now_kst.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    rows = con.execute("SELECT points, created_at, mission_id FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchall()
    # 월 상한은 '적립'만 센다 — 교환 차감(음수)과 반려 환급(refund)은 제외
    return sum(p for p, ts, mid in rows if p > 0 and mid != "refund" and datetime.fromisoformat(ts) >= month_start)


def _awarded(con: sqlite3.Connection, ev_id: str, mission_id: str, key: str):
    return con.execute("SELECT points FROM point_ledger WHERE ev_id=? AND mission_id=? AND period_key=?",
                       (ev_id, mission_id, key)).fetchone()


def evaluate(ev_id: str, username: str, lang: str = "ko", now_kst: datetime | None = None) -> dict:
    """미션 진행을 계산하고, 달성했고 동의가 있으면 포인트를 적립한 뒤 현재 상태를 돌려준다."""
    now_kst = now_kst or kst_now()
    cfg = load_config()
    policy = IncentivePolicy.load()
    lang = "en" if lang == "en" else "ko"
    opted_in = consent.has_consent(ev_id, "incentive")
    guide_rows = _guide_rows(username)
    con = _connect()
    try:
        earned_month = _month_earned(con, ev_id, now_kst)
        cap = policy.monthly_cap_points
        missions = []
        for m in cfg["missions"]:
            key = _period_key(m["period"], now_kst)
            verification = None
            if m["metric"] == "verified_follow":
                progress, verification = _follow_status(con, ev_id, now_kst, cfg["verification"])
            else:
                progress = _progress(m["metric"], m["period"], ev_id, username, now_kst, guide_rows)
            achieved = progress >= m["target"]
            if achieved and verification:
                verification = "ok"
            awarded = _awarded(con, ev_id, m["id"], key)
            if achieved and opted_in and not awarded:
                grant = m["points"] if cap is None else max(0, min(m["points"], int(cap) - earned_month))
                if grant > 0:
                    try:
                        con.execute("INSERT INTO point_ledger (ev_id, mission_id, period_key, points, created_at) VALUES (?,?,?,?,?)",
                                    (ev_id, m["id"], key, grant, datetime.now(timezone.utc).isoformat()))
                        con.commit()
                        earned_month += grant
                        awarded = (grant,)
                        audit.log(username, "reward_earn", target=ev_id, detail={"mission": m["id"], "period": key, "points": grant})
                    except sqlite3.IntegrityError:   # 동시 요청이 먼저 적립한 경우
                        awarded = _awarded(con, ev_id, m["id"], key)
            missions.append({
                "id": m["id"], "type": m["type"], "period": m["period"], "period_key": key,
                "title": m["title"][lang], "desc": m["desc"][lang],
                "target": m["target"], "progress": min(progress, m["target"]), "achieved": achieved,
                "points": m["points"], "awarded_points": awarded[0] if awarded else None,
                "verification": verification,
            })
        balance = con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0]
    finally:
        con.close()
    return {
        "missions": missions,
        "points": {"balance": balance, "month_earned": earned_month, "month_cap": cap},
        "incentive_consent": opted_in,
        "verified": bool(cfg.get("verified")) and policy.verified,
        "note": NOTE,
    }


def ledger(ev_id: str, limit: int = 50) -> list[dict]:
    con = _connect()
    try:
        rows = con.execute("SELECT mission_id, period_key, points, created_at FROM point_ledger WHERE ev_id=? ORDER BY id DESC LIMIT ?",
                           (ev_id, limit)).fetchall()
    finally:
        con.close()
    return [{"mission_id": a, "period_key": b, "points": c, "created_at": d} for a, b, c, d in rows]
