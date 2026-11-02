"""어드바이저리(추천 안내) 기록 — 차주에게 낸 충·방전 추천과 차주의 응답을 남긴다.

계획서 4.1 라. "EV별 AI 추천 충전 시간 추론 및 산출" 결과를 모바일앱·관리자 화면으로 제공한 기록이다.
추천을 "보냈다"와 "따랐다"를 분리해 기록해 두면 2차년도 인센티브 모델 설계(4.3 라.)의 근거로 쓸 수 있다.
채널(dashboard/app/api)은 전달 경로만 표시한다.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from src.pipeline import store

CHANNELS = ("dashboard", "app", "api")
RESPONSES = ("accepted", "declined", "expired")


class AdvisoryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def issue(ev_id: str, plan: dict, channel: str = "api", inputs: dict | None = None,
          con: sqlite3.Connection | None = None) -> str:
    if channel not in CHANNELS:
        raise AdvisoryError(f"channel은 {CHANNELS} 중 하나여야 합니다: {channel}")
    advisory_id = "ADV-" + uuid.uuid4().hex[:12]
    verified = bool((plan.get("assumptions") or {}).get("verified", False))
    own = con is None
    con = con or store.get_connection()
    try:
        con.execute(
            "INSERT INTO advisories (advisory_id, ev_id, created_at, channel, inputs, plan, net_krw, verified) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (advisory_id, ev_id, _now(), channel, json.dumps(inputs or {}, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False), plan.get("net_krw"), int(verified)),
        )
        con.commit()
    finally:
        if own:
            con.close()
    return advisory_id


def respond(advisory_id: str, status: str, ev_id: str | None = None, con: sqlite3.Connection | None = None) -> None:
    """발행(issued) 상태의 추천에만 응답할 수 있다 — 이미 응답한 건은 바꾸지 않는다.
    ev_id를 주면 그 차량의 추천에만 응답한다(이용자 본인 응답용) — 다른 차량의 추천은 '없는 추천'으로 본다."""
    if status not in RESPONSES:
        raise AdvisoryError(f"status는 {RESPONSES} 중 하나여야 합니다: {status}")
    own = con is None
    con = con or store.get_connection()
    try:
        q, args = "UPDATE advisories SET status=?, responded_at=? WHERE advisory_id=? AND status='issued'", [status, _now(), advisory_id]
        if ev_id is not None:
            q += " AND ev_id=?"
            args.append(ev_id)
        cur = con.execute(q, args)
        con.commit()
        if cur.rowcount == 0:
            exists = con.execute("SELECT status, ev_id FROM advisories WHERE advisory_id=?", (advisory_id,)).fetchone()
            if exists and ev_id is not None and exists[1] != ev_id:
                exists = None
            if not exists:
                raise AdvisoryError(f"없는 추천입니다: {advisory_id}")
            raise AdvisoryError(f"이미 응답된 추천입니다({exists[0]})")
    finally:
        if own:
            con.close()


def list_advisories(ev_id: str | None = None, limit: int = 50, con: sqlite3.Connection | None = None) -> list[dict]:
    own = con is None
    con = con or store.get_connection()
    try:
        cols = ["advisory_id", "ev_id", "created_at", "channel", "net_krw", "verified", "status", "responded_at"]
        q = f"SELECT {', '.join(cols)} FROM advisories"
        args: list = []
        if ev_id:
            q += " WHERE ev_id=?"
            args.append(ev_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(zip(cols, r)) for r in con.execute(q, args)]
    finally:
        if own:
            con.close()


def acceptance_stats(con: sqlite3.Connection | None = None) -> dict:
    """응답 완료 건 중 수용 비율과 상태별 건수. 응답 전(issued) 건은 비율 분모에서 제외한다."""
    own = con is None
    con = con or store.get_connection()
    try:
        counts = dict(con.execute("SELECT status, COUNT(*) FROM advisories GROUP BY status").fetchall())
    finally:
        if own:
            con.close()
    answered = counts.get("accepted", 0) + counts.get("declined", 0)
    return {
        "counts": counts,
        "acceptance_rate": round(counts.get("accepted", 0) / answered, 3) if answered else None,
    }
