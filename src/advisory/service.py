"""어드바이저리(추천 안내) 기록 — 차주에게 낸 충·방전 추천과 차주의 응답을 남긴다.

추천을 "보냈다"와 "따랐다"를 분리해 기록해야 수용률·실제 절감 효과를 평가할 수 있다
(WP11 실험·인센티브 분석의 기초 데이터). 채널(dashboard/app/kakao/sms/api)은 전달 경로만 표시하며,
실제 메신저 연동은 별도 어댑터가 `issue(channel=...)`로 기록한다.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from src.pipeline import store

CHANNELS = ("dashboard", "app", "kakao", "sms", "api")
RESPONSES = ("accepted", "declined", "expired")


class AdvisoryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def issue(ev_id: str, plan: dict, channel: str = "api", inputs: dict | None = None,
          group_id: str | None = None, con: sqlite3.Connection | None = None) -> str:
    if channel not in CHANNELS:
        raise AdvisoryError(f"channel은 {CHANNELS} 중 하나여야 합니다: {channel}")
    advisory_id = "ADV-" + uuid.uuid4().hex[:12]
    verified = bool((plan.get("assumptions") or {}).get("verified", False))
    own = con is None
    con = con or store.get_connection()
    try:
        con.execute(
            "INSERT INTO advisories (advisory_id, ev_id, created_at, channel, inputs, plan, net_krw, verified, group_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (advisory_id, ev_id, _now(), channel, json.dumps(inputs or {}, ensure_ascii=False),
             json.dumps(plan, ensure_ascii=False), plan.get("net_krw"), int(verified), group_id),
        )
        con.commit()
    finally:
        if own:
            con.close()
    return advisory_id


def respond(advisory_id: str, status: str, con: sqlite3.Connection | None = None) -> None:
    """발행(issued) 상태의 추천에만 응답할 수 있다 — 이미 응답한 건은 바꾸지 않는다."""
    if status not in RESPONSES:
        raise AdvisoryError(f"status는 {RESPONSES} 중 하나여야 합니다: {status}")
    own = con is None
    con = con or store.get_connection()
    try:
        cur = con.execute(
            "UPDATE advisories SET status=?, responded_at=? WHERE advisory_id=? AND status='issued'",
            (status, _now(), advisory_id),
        )
        con.commit()
        if cur.rowcount == 0:
            exists = con.execute("SELECT status FROM advisories WHERE advisory_id=?", (advisory_id,)).fetchone()
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
        cols = ["advisory_id", "ev_id", "created_at", "channel", "net_krw", "verified", "status", "responded_at", "group_id"]
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
