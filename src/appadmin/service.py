"""웹앱 관리(어드민) — 서포터즈 공지사항, 앱 안내 문구.

관리자가 PC에서 입력하고 이용자(서포터즈) 모바일 앱이 읽는다. 저장소는 `privacy.db`(동의·감사로그와 같은 파일)를 쓴다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.privacy import consent

MAX_TITLE, MAX_BODY, MAX_TEXT = 80, 2000, 600

# 관리자가 바꿀 수 있는 앱 문구 — 모바일 언어팩의 키만 허용한다(임의 키·스크립트 주입 방지: 화면에서 textContent로만 쓴다)
EDITABLE_KEYS = {
    "home_intro": "홈 — 서포터즈 참여 안내 문구",
    "guide_note": "충전 시간 — 추천 안내 문구",
    "data_note": "데이터 수집 안내 문구",
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
            key TEXT PRIMARY KEY, ko TEXT, en TEXT, updated_by TEXT, updated_at TEXT NOT NULL);"""
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
