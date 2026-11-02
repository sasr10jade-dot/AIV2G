"""참여자(차량) 동의 이력 — 목적별로 부여/철회를 기록한다.

목적(purpose)
- collect         OBD·충전 데이터 수집
- share_external  외부(공공기관·사업자·LLM) 제공 시 가명 데이터 사용
- incentive       인센티브·정산 목적 이용
- research        연구·분석 목적 이용
- notify          모바일웹 알림 수신(충전 요금 시간대·수요예측·충·방전 추천 안내)
- profile         가입 시 받은 페르소나 정보(차종·직업군·거주지) 수집·이용 — 맞춤 안내 목적

이력은 지우지 않는다: 철회도 행의 withdrawn_at으로 남기고, 현재 유효 여부는
withdrawn_at이 비어 있는 행이 있는지로 판단한다. 동의 문구 버전(version)을 함께 저장해
어떤 문구에 동의했는지 추적한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from src.paths import DATA_DIR

PRIVACY_DB = DATA_DIR / "privacy.db"
PURPOSES = ("collect", "share_external", "incentive", "research", "notify", "profile", "v2g_discharge")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    PRIVACY_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(PRIVACY_DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS consents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ev_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            version TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            withdrawn_at TEXT,
            recorded_by TEXT
        )"""
    )
    return con


def _check(purpose: str) -> None:
    if purpose not in PURPOSES:
        raise ValueError(f"알 수 없는 동의 목적: {purpose}")


def has_consent(ev_id: str, purpose: str, con: sqlite3.Connection | None = None) -> bool:
    _check(purpose)
    own = con is None
    con = con or connect()
    try:
        row = con.execute(
            "SELECT 1 FROM consents WHERE ev_id=? AND purpose=? AND withdrawn_at IS NULL LIMIT 1",
            (ev_id, purpose),
        ).fetchone()
        return row is not None
    finally:
        if own:
            con.close()


def grant(ev_id: str, purpose: str, version: str, recorded_by: str | None = None) -> None:
    _check(purpose)
    con = connect()
    try:
        if has_consent(ev_id, purpose, con=con):
            return  # 이미 유효 — 중복 행을 만들지 않는다
        con.execute(
            "INSERT INTO consents (ev_id, purpose, version, granted_at, recorded_by) VALUES (?,?,?,?,?)",
            (ev_id, purpose, version, _now(), recorded_by),
        )
        con.commit()
    finally:
        con.close()


def withdraw(ev_id: str, purpose: str) -> bool:
    """유효한 동의가 있으면 철회 시각을 기록하고 True, 없으면 False."""
    _check(purpose)
    con = connect()
    try:
        cur = con.execute(
            "UPDATE consents SET withdrawn_at=? WHERE ev_id=? AND purpose=? AND withdrawn_at IS NULL",
            (_now(), ev_id, purpose),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def list_consents(ev_id: str | None = None) -> list[dict]:
    cols = ["ev_id", "purpose", "version", "granted_at", "withdrawn_at", "recorded_by"]
    con = connect()
    try:
        q = f"SELECT {', '.join(cols)} FROM consents"
        args: tuple = ()
        if ev_id:
            q += " WHERE ev_id=?"
            args = (ev_id,)
        return [dict(zip(cols, r)) for r in con.execute(q + " ORDER BY id", args)]
    finally:
        con.close()
