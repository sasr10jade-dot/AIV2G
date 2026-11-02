"""감사로그 — 누가·언제·무엇을 했는지 해시체인으로 남겨 사후 변조를 탐지한다.

각 행의 hash = SHA-256(이전 행 hash + 이번 행 내용). 중간 행을 고치거나 지우면 이후 모든
행의 체인이 어긋나 `verify_chain()`이 첫 불일치 위치를 알려 준다. 로그에는 원본 번호판 등
식별정보를 넣지 않는다(호출 측이 가명 ID나 마스킹 값을 넘긴다).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from src.privacy import consent

GENESIS = "0" * 64


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    con = consent.connect()  # 같은 privacy.db를 쓴다(경로는 consent.PRIVACY_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            detail TEXT,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL
        )"""
    )
    # 오래된 기록을 파기해도 체인을 검증할 수 있게, 마지막으로 지운 행의 해시를 기준점으로 남긴다
    con.execute(
        """CREATE TABLE IF NOT EXISTS audit_checkpoint (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_hash TEXT NOT NULL,
            purged_through_id INTEGER NOT NULL,
            purged_total INTEGER NOT NULL,
            purged_at TEXT NOT NULL
        )"""
    )
    con.commit()
    return con


def _digest(prev: str, ts: str, actor: str, action: str, target: str | None, detail: str | None) -> str:
    payload = json.dumps([prev, ts, actor, action, target, detail], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log(actor: str, action: str, target: str | None = None, detail: dict | None = None) -> None:
    detail_s = json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")  # 동시 기록 시 체인이 갈라지지 않도록 직렬화
        row = con.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            prev = row[0]
        else:  # 기록을 전부 파기한 뒤에도 체크포인트에서 체인을 이어간다
            cp = con.execute("SELECT last_hash FROM audit_checkpoint WHERE id=1").fetchone()
            prev = cp[0] if cp else GENESIS
        ts = _now()
        con.execute(
            "INSERT INTO audit_log (ts, actor, action, target, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, actor, action, target, detail_s, prev, _digest(prev, ts, actor, action, target, detail_s)),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def verify_chain() -> dict:
    """{"ok": bool, "rows": n, "broken_at": id | None}"""
    con = _connect()
    try:
        cp = con.execute("SELECT last_hash FROM audit_checkpoint WHERE id=1").fetchone()
        prev = cp[0] if cp else GENESIS
        n = 0
        for id_, ts, actor, action, target, detail, p, h in con.execute(
            "SELECT id, ts, actor, action, target, detail, prev_hash, hash FROM audit_log ORDER BY id"
        ):
            n += 1
            if p != prev or h != _digest(p, ts, actor, action, target, detail):
                return {"ok": False, "rows": n, "broken_at": id_}
            prev = h
        return {"ok": True, "rows": n, "broken_at": None}
    finally:
        con.close()


def purge_before(cutoff_iso: str) -> int:
    """cutoff 이전의 가장 오래된 기록을 지운다(보관 기간 경과분 파기). 앞에서부터 이어진 구간만 지우고,
    마지막으로 지운 행의 해시를 체크포인트로 남겨 이후 체인 검증이 계속 가능하게 한다. 지운 건수를 돌려준다."""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        last_id = last_hash = None
        n = 0
        for id_, ts, h in con.execute("SELECT id, ts, hash FROM audit_log ORDER BY id"):
            if ts >= cutoff_iso:
                break
            last_id, last_hash, n = id_, h, n + 1
        if n == 0:
            con.rollback()
            return 0
        prior = con.execute("SELECT purged_total FROM audit_checkpoint WHERE id=1").fetchone()
        con.execute("DELETE FROM audit_log WHERE id <= ?", (last_id,))
        con.execute(
            "INSERT INTO audit_checkpoint (id, last_hash, purged_through_id, purged_total, purged_at) VALUES (1,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET last_hash=excluded.last_hash, purged_through_id=excluded.purged_through_id, "
            "purged_total=excluded.purged_total, purged_at=excluded.purged_at",
            (last_hash, last_id, (prior[0] if prior else 0) + n, _now()),
        )
        con.commit()
        return n
    finally:
        con.close()


def count_before(cutoff_iso: str) -> int:
    """purge_before가 지울 건수(앞에서부터 이어진 구간) — 실제로 지우기 전에 확인하는 용도."""
    con = _connect()
    try:
        n = 0
        for (ts,) in con.execute("SELECT ts FROM audit_log ORDER BY id"):
            if ts >= cutoff_iso:
                break
            n += 1
        return n
    finally:
        con.close()


def entries(action_prefix: str | None = None, target: str | None = None, limit: int = 100) -> list[dict]:
    """조건에 맞는 기록을 최신순으로 돌려준다(예: 특정 회원의 member.* 이력). 접두 일치는 LIKE 와일드카드를
    쓰지 않고 SUBSTR로 비교한다 — 호출 측 입력이 패턴으로 해석되지 않게."""
    cols = ["id", "ts", "actor", "action", "target", "detail"]
    q, args = f"SELECT {', '.join(cols)} FROM audit_log WHERE 1=1", []
    if action_prefix:
        q += " AND SUBSTR(action, 1, ?) = ?"
        args += [len(action_prefix), action_prefix]
    if target is not None:
        q += " AND target = ?"
        args.append(target)
    con = _connect()
    try:
        rows = con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def recent(limit: int = 50) -> list[dict]:
    cols = ["id", "ts", "actor", "action", "target", "detail"]
    con = _connect()
    try:
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()
