"""웹앱 관리(어드민) — 공지사항, 앱 문구(미션 안내 등), 상품권 교환·코드 등록.

세 가지 모두 관리자가 PC에서 입력하고 이용자 모바일 앱이 읽는다. 저장소는 `privacy.db`(동의·감사로그와 같은 파일)를 쓴다.
상품권 코드는 금전적 가치가 있어 번호판과 같은 방식(Fernet)으로 암호화해 저장하고, 감사로그에는 코드 값을 남기지 않는다.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.paths import PROJECT_ROOT
from src.privacy import consent, vault
from src.rewards import service as rewards

REWARDS_CONFIG = PROJECT_ROOT / "config" / "rewards.json"
MAX_TITLE, MAX_BODY, MAX_TEXT = 80, 2000, 600

# 관리자가 바꿀 수 있는 앱 문구 — 모바일 언어팩의 키만 허용한다(임의 키·스크립트 주입 방지: 화면에서 textContent로만 쓴다)
EDITABLE_KEYS = {
    "ms_intro_lead": "미션 소개 — 첫 문장",
    "ms_step1_d": "미션 소개 — 1단계 설명", "ms_step2_d": "미션 소개 — 2단계 설명", "ms_step3_d": "미션 소개 — 3단계 설명",
    "ms_intro_safe": "미션 소개 — 안전·상품권 안내",
    "ms_note": "시범 포인트 안내 문구",
}


class AdminError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    con = consent.connect()
    con.executescript(
        """CREATE TABLE IF NOT EXISTS app_notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 1,
            created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_content (
            key TEXT PRIMARY KEY, ko TEXT, en TEXT, updated_by TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ev_id TEXT NOT NULL, points INTEGER NOT NULL, status TEXT NOT NULL,
            requested_at TEXT NOT NULL, handled_by TEXT, handled_at TEXT, code_enc TEXT, label TEXT, reason TEXT);"""
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(app_notices)")}
    for col in ("title_en", "body_en"):   # 영어 공지(선택) — 기존 DB에는 칸을 더해 준다
        if col not in cols:
            con.execute(f"ALTER TABLE app_notices ADD COLUMN {col} TEXT")
    con.commit()
    return con


# ------------------------------------------------------------------ 공지사항 --

def _notice(row, lang: str | None = None) -> dict:
    out = {"id": row[0], "title": row[1], "body": row[2], "pinned": bool(row[3]), "published": bool(row[4]),
           "created_by": row[5], "created_at": row[6], "updated_at": row[7], "title_en": row[8], "body_en": row[9]}
    if lang == "en" and row[8] and row[9]:   # 영어 제목·내용이 모두 있을 때만 영어로 보여 준다
        out["title"], out["body"] = row[8], row[9]
    return out


_NOTICE_COLS = "id, title, body, pinned, published, created_by, created_at, updated_at, title_en, body_en"


def _check_notice(title: str, body: str) -> tuple[str, str]:
    title, body = (title or "").strip(), (body or "").strip()
    if not title or len(title) > MAX_TITLE:
        raise AdminError(f"제목은 1~{MAX_TITLE}자로 입력하세요")
    if not body or len(body) > MAX_BODY:
        raise AdminError(f"내용은 1~{MAX_BODY}자로 입력하세요")
    return title, body


def _check_en(title_en: str | None, body_en: str | None) -> tuple[str | None, str | None]:
    title_en, body_en = (title_en or "").strip() or None, (body_en or "").strip() or None
    if bool(title_en) != bool(body_en):
        raise AdminError("영어 공지는 제목과 내용을 함께 입력하세요")
    if (title_en and len(title_en) > MAX_TITLE) or (body_en and len(body_en) > MAX_BODY):
        raise AdminError(f"영어 제목은 {MAX_TITLE}자, 내용은 {MAX_BODY}자 이하로 입력하세요")
    return title_en, body_en


def notice_create(title: str, body: str, pinned: bool, published: bool, actor: str,
                  title_en: str | None = None, body_en: str | None = None) -> int:
    title, body = _check_notice(title, body)
    title_en, body_en = _check_en(title_en, body_en)
    con = _connect()
    try:
        now = _now()
        cur = con.execute("INSERT INTO app_notices (title, body, pinned, published, created_by, created_at, updated_at, title_en, body_en) VALUES (?,?,?,?,?,?,?,?,?)",
                          (title, body, int(pinned), int(published), actor, now, now, title_en, body_en))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def notice_update(notice_id: int, title: str, body: str, pinned: bool, published: bool,
                  title_en: str | None = None, body_en: str | None = None) -> None:
    title, body = _check_notice(title, body)
    title_en, body_en = _check_en(title_en, body_en)
    con = _connect()
    try:
        cur = con.execute("UPDATE app_notices SET title=?, body=?, pinned=?, published=?, updated_at=?, title_en=?, body_en=? WHERE id=?",
                          (title, body, int(pinned), int(published), _now(), title_en, body_en, notice_id))
        con.commit()
        if not cur.rowcount:
            raise AdminError("공지를 찾을 수 없습니다")
    finally:
        con.close()


def notice_delete(notice_id: int) -> None:
    con = _connect()
    try:
        cur = con.execute("DELETE FROM app_notices WHERE id=?", (notice_id,))
        con.commit()
        if not cur.rowcount:
            raise AdminError("공지를 찾을 수 없습니다")
    finally:
        con.close()


def notices(published_only: bool, limit: int = 50, lang: str | None = None) -> list[dict]:
    con = _connect()
    try:
        where = "WHERE published=1" if published_only else ""
        rows = con.execute(f"SELECT {_NOTICE_COLS} FROM app_notices {where} ORDER BY pinned DESC, id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [_notice(r, lang) for r in rows]


# ------------------------------------------------------------------ 앱 문구 --

def content_set(key: str, ko: str | None, en: str | None, actor: str) -> None:
    if key not in EDITABLE_KEYS:
        raise AdminError("수정할 수 없는 문구입니다")
    ko, en = (ko or "").strip() or None, (en or "").strip() or None
    if not ko and not en:
        raise AdminError("한국어 또는 영어 문구를 입력하세요")
    if any(v and len(v) > MAX_TEXT for v in (ko, en)):
        raise AdminError(f"문구는 {MAX_TEXT}자 이하로 입력하세요")
    con = _connect()
    try:
        con.execute("INSERT INTO app_content (key, ko, en, updated_by, updated_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET ko=excluded.ko, en=excluded.en, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                    (key, ko, en, actor, _now()))
        con.commit()
    finally:
        con.close()


def content_reset(key: str) -> None:
    if key not in EDITABLE_KEYS:
        raise AdminError("수정할 수 없는 문구입니다")
    con = _connect()
    try:
        con.execute("DELETE FROM app_content WHERE key=?", (key,))
        con.commit()
    finally:
        con.close()


def content_overrides() -> dict:
    con = _connect()
    try:
        rows = con.execute("SELECT key, ko, en FROM app_content").fetchall()
    finally:
        con.close()
    return {k: {"ko": ko, "en": en} for k, ko, en in rows if k in EDITABLE_KEYS}


# ------------------------------------------------------------------ 상품권 교환 --

def reward_config() -> dict:
    return json.loads(Path(REWARDS_CONFIG).read_text(encoding="utf-8"))


def _balance(con: sqlite3.Connection, ev_id: str) -> int:
    return con.execute("SELECT COALESCE(SUM(points),0) FROM point_ledger WHERE ev_id=?", (ev_id,)).fetchone()[0]


def redeem_state(ev_id: str) -> dict:
    cfg = reward_config()
    rewards_con = rewards._connect()   # point_ledger 보장
    rewards_con.close()
    con = _connect()
    try:
        balance = _balance(con, ev_id)
        pending = con.execute("SELECT COUNT(*) FROM redemptions WHERE ev_id=? AND status='requested'", (ev_id,)).fetchone()[0]
    finally:
        con.close()
    th = int(cfg["threshold_points"])
    return {"enabled": bool(cfg["redeem_enabled"]), "threshold": th, "label": cfg["label"], "balance": balance,
            "can_request": bool(cfg["redeem_enabled"]) and balance >= th and not pending, "pending": pending,
            "verified": bool(cfg.get("verified"))}


def redeem_request(ev_id: str) -> int:
    """교환을 신청한다 — 포인트는 신청 즉시 차감해 두고, 반려되면 되돌려 준다(중복 신청 방지)."""
    cfg = reward_config()
    if not cfg["redeem_enabled"]:
        raise AdminError("지금은 교환을 받지 않아요")
    th = int(cfg["threshold_points"])
    rewards._connect().close()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        if con.execute("SELECT 1 FROM redemptions WHERE ev_id=? AND status='requested'", (ev_id,)).fetchone():
            raise AdminError("이미 처리 중인 교환 신청이 있어요")
        if _balance(con, ev_id) < th:
            raise AdminError(f"{th:,}포인트가 모이면 교환할 수 있어요")
        cur = con.execute("INSERT INTO redemptions (ev_id, points, status, requested_at) VALUES (?,?,?,?)", (ev_id, th, "requested", _now()))
        rid = cur.lastrowid
        con.execute("INSERT INTO point_ledger (ev_id, mission_id, period_key, points, created_at) VALUES (?,?,?,?,?)",
                    (ev_id, "redeem", str(rid), -th, _now()))
        con.commit()
        return rid
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def redemptions_admin(status: str | None = None, limit: int = 200) -> list[dict]:
    con = _connect()
    try:
        q = "SELECT id, ev_id, points, status, requested_at, handled_by, handled_at, label, reason FROM redemptions"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        rows = con.execute(q + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()   # 코드 값은 목록에 싣지 않는다
    finally:
        con.close()
    keys = ("id", "ev_id", "points", "status", "requested_at", "handled_by", "handled_at", "label", "reason")
    return [dict(zip(keys, r)) for r in rows]


def redeem_fulfill(rid: int, code: str, label: str | None, actor: str) -> None:
    code = (code or "").strip()
    if not code or len(code) > 200:
        raise AdminError("상품권 코드를 입력하세요(200자 이하)")
    con = _connect()
    try:
        cur = con.execute("UPDATE redemptions SET status='fulfilled', code_enc=?, label=?, handled_by=?, handled_at=? WHERE id=? AND status='requested'",
                          (vault.encrypt(code), (label or reward_config()["label"]).strip()[:40], actor, _now(), rid))
        con.commit()
        if not cur.rowcount:
            raise AdminError("처리할 수 있는 신청이 아니에요(이미 처리됐거나 없음)")
    finally:
        con.close()


def redeem_reject(rid: int, reason: str, actor: str) -> None:
    reason = (reason or "").strip()
    if len(reason) < 2 or len(reason) > 200:
        raise AdminError("반려 사유를 2~200자로 입력하세요")
    rewards._connect().close()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT ev_id, points FROM redemptions WHERE id=? AND status='requested'", (rid,)).fetchone()
        if not row:
            raise AdminError("처리할 수 있는 신청이 아니에요(이미 처리됐거나 없음)")
        con.execute("UPDATE redemptions SET status='rejected', reason=?, handled_by=?, handled_at=? WHERE id=?", (reason, actor, _now(), rid))
        con.execute("INSERT INTO point_ledger (ev_id, mission_id, period_key, points, created_at) VALUES (?,?,?,?,?)",
                    (row[0], "refund", str(rid), row[1], _now()))   # 반려하면 차감한 포인트를 돌려준다
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


EN_DEFAULT_LABEL = "Cultural gift certificate"


def coupons(ev_id: str, lang: str | None = None) -> list[dict]:
    """이용자 본인의 교환 내역 — 발급된 코드는 본인에게만 복호화해 보여 준다."""
    con = _connect()
    try:
        rows = con.execute("SELECT id, points, status, requested_at, handled_at, code_enc, label, reason FROM redemptions "
                           "WHERE ev_id=? ORDER BY id DESC", (ev_id,)).fetchall()
    finally:
        con.close()
    out = []
    for rid, pts, status, req, handled, enc, label, reason in rows:
        if lang == "en" and label == reward_config()["label"]:   # 기본 이름이면 영어 이름으로 보여 준다
            label = EN_DEFAULT_LABEL
        code, code_error = None, False
        if status == "fulfilled" and enc:
            try:
                code = vault.decrypt(enc)
            except vault.VaultError:   # 암호화 키가 바뀌었거나 값이 손상됐다 — 쿠폰함 전체가 실패하지 않게 표시만 남긴다
                code_error = True
        out.append({"id": rid, "points": pts, "status": status, "requested_at": req, "handled_at": handled, "label": label,
                    "reason": reason if status == "rejected" else None, "code": code, "code_error": code_error})
    return out
