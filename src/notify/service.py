"""차량 등록자 알림함 — 알림 생성·조회·읽음 처리.

원칙
- 알림은 수신 동의(`notify`)가 있는 차량에만 만든다. 동의가 없거나 철회됐으면 저장도, 발송도 하지 않는다.
- 같은 (차량, dedupe_key) 알림은 한 번만 만든다 — 주기 작업이 반복돼도 알림이 쌓이지 않는다.
- 조회·읽음 처리는 항상 ev_id로 범위를 좁힌다. 알림 ID만 알아도 남의 알림은 건드릴 수 없다.
- 동의를 철회해도 이미 받은 알림은 볼 수 있다(새 알림만 멈춘다).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from src.pipeline import store
from src.privacy import consent

KINDS = ("price_window", "forecast", "advisory", "system")
NOTIFY_CONSENT_VERSION = "notify-v1"
MAX_TITLE = 80
MAX_BODY = 500


class NotifyError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(
    ev_id: str, kind: str, title: str, body: str, payload: dict | None = None,
    dedupe_key: str | None = None, con: sqlite3.Connection | None = None,
) -> str | None:
    """알림을 만들고 ID를 돌려준다. 수신 동의가 없거나 같은 dedupe_key가 이미 있으면 None."""
    if kind not in KINDS:
        raise NotifyError(f"kind는 {KINDS} 중 하나여야 합니다: {kind}")
    title, body = (title or "").strip(), (body or "").strip()
    if not title or not body:
        raise NotifyError("title과 body는 비어 있을 수 없습니다")
    if len(title) > MAX_TITLE or len(body) > MAX_BODY:
        raise NotifyError(f"title은 {MAX_TITLE}자, body는 {MAX_BODY}자 이하여야 합니다")
    if not consent.has_consent(ev_id, "notify"):
        return None

    notification_id = "NTF-" + uuid.uuid4().hex[:12]
    own = con is None
    con = con or store.get_connection()
    try:
        try:
            con.execute(
                "INSERT INTO notifications (notification_id, ev_id, created_at, kind, title, body, payload, dedupe_key) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (notification_id, ev_id, _now(), kind, title, body,
                 json.dumps(payload, ensure_ascii=False) if payload else None, dedupe_key),
            )
            con.commit()
        except sqlite3.IntegrityError:  # (ev_id, dedupe_key) 중복
            con.rollback()
            return None
    finally:
        if own:
            con.close()
    return notification_id


def list_for(ev_id: str, limit: int = 50, unread_only: bool = False, con: sqlite3.Connection | None = None) -> list[dict]:
    own = con is None
    con = con or store.get_connection()
    try:
        q = ("SELECT notification_id, created_at, kind, title, body, payload, read_at FROM notifications "
             "WHERE ev_id=?" + (" AND read_at IS NULL" if unread_only else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?")
        rows = con.execute(q, (ev_id, limit)).fetchall()
    finally:
        if own:
            con.close()
    return [
        {"notification_id": r[0], "created_at": r[1], "kind": r[2], "title": r[3], "body": r[4],
         "payload": json.loads(r[5]) if r[5] else None, "read": r[6] is not None}
        for r in rows
    ]


def unread_count(ev_id: str, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or store.get_connection()
    try:
        return int(con.execute("SELECT COUNT(*) FROM notifications WHERE ev_id=? AND read_at IS NULL", (ev_id,)).fetchone()[0])
    finally:
        if own:
            con.close()


def mark_read(ev_id: str, notification_id: str, con: sqlite3.Connection | None = None) -> bool:
    """본인 차량의 안 읽은 알림이면 읽음 처리하고 True. 없거나 남의 알림이거나 이미 읽었으면 False."""
    own = con is None
    con = con or store.get_connection()
    try:
        cur = con.execute(
            "UPDATE notifications SET read_at=? WHERE notification_id=? AND ev_id=? AND read_at IS NULL",
            (_now(), notification_id, ev_id),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        if own:
            con.close()


def mark_all_read(ev_id: str, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or store.get_connection()
    try:
        cur = con.execute("UPDATE notifications SET read_at=? WHERE ev_id=? AND read_at IS NULL", (_now(), ev_id))
        con.commit()
        return cur.rowcount
    finally:
        if own:
            con.close()


def count_all(ev_id: str, con: sqlite3.Connection | None = None) -> int:
    own = con is None
    con = con or store.get_connection()
    try:
        return int(con.execute("SELECT COUNT(*) FROM notifications WHERE ev_id=?", (ev_id,)).fetchone()[0])
    finally:
        if own:
            con.close()
