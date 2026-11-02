"""방전(V2G/V2L) 이벤트 기록 — KPI 'B 방전 이벤트' 실적의 근거 테이블(discharge_events)에 쓴다.

- event_id는 (ev_id, charger_id, start_ts)의 해시라 같은 사건을 두 번 기록해도 한 행만 남는다.
- kind: V2G(계통 역송전) | V2L(부하 공급) | TEST(에뮬레이터·벤치 시험). KPI 실적에는 V2G/V2L만 센다.
- OCPP 세션 종료 시 자동 기록되는 종류는 환경변수 DISCHARGE_RECORD_KIND(기본 TEST)로 정한다 —
  에뮬레이터 연결이 실적을 부풀리지 않도록 기본값은 TEST이고, 실차·실충전기 운영 시에만 V2G로 바꾼다.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

from src.pipeline import store

KINDS = ("V2G", "V2L", "TEST")
MIN_EVENT_KWH = 0.1  # 이보다 적은 방전은 계측 잡음으로 보고 기록하지 않는다


def make_event_id(ev_id: str, charger_id: str | None, start_ts: str) -> str:
    raw = f"{ev_id}|{charger_id or ''}|{start_ts}".encode("utf-8")
    return "DE-" + hashlib.sha256(raw).hexdigest()[:16]


def record_discharge_event(
    ev_id: str,
    start_ts: str,
    duration_h: float,
    energy_kwh: float,
    kind: str,
    charger_id: str | None = None,
    source: str | None = None,
    con: sqlite3.Connection | None = None,
) -> bool:
    """기록했으면 True, 잡음 수준이라 건너뛰었거나 이미 있으면 False."""
    if kind not in KINDS:
        raise ValueError(f"kind는 {KINDS} 중 하나여야 합니다: {kind}")
    if duration_h < 0 or energy_kwh < 0:
        raise ValueError("duration_h, energy_kwh는 0 이상이어야 합니다")
    if energy_kwh < MIN_EVENT_KWH:
        return False
    own = con is None
    con = con or store.get_connection()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO discharge_events "
            "(event_id, ev_id, charger_id, start_ts, duration_h, energy_kwh, kind, source) VALUES (?,?,?,?,?,?,?,?)",
            (make_event_id(ev_id, charger_id, start_ts), ev_id, charger_id, start_ts,
             round(duration_h, 3), round(energy_kwh, 3), kind, source),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        if own:
            con.close()


def default_kind() -> str:
    kind = os.environ.get("DISCHARGE_RECORD_KIND", "TEST")
    return kind if kind in KINDS else "TEST"


def list_events(ev_id: str | None = None, limit: int = 100, con: sqlite3.Connection | None = None) -> list[dict]:
    own = con is None
    con = con or store.get_connection()
    try:
        cols = ["event_id", "ev_id", "charger_id", "start_ts", "duration_h", "energy_kwh", "kind", "source"]
        q = f"SELECT {', '.join(cols)} FROM discharge_events"
        args: list = []
        if ev_id:
            q += " WHERE ev_id=?"
            args.append(ev_id)
        q += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        return [dict(zip(cols, r)) for r in con.execute(q, args)]
    finally:
        if own:
            con.close()
