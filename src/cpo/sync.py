"""CPO 세션 동기화 — 어댑터가 가져온 세션을 curated.db의 charger_sessions에 안전하게 합친다.

- 전역 session_id = "<operator_id>:<사업자 session_id>" (사업자 간 ID 충돌 방지)
- 같은 세션을 다시 받으면 덮어쓰기(멱등) — 중복 행이 생기지 않는다
- 한 사업자의 오류(형식 불량 등)는 그 사업자만 실패 처리하고 다른 사업자는 계속 진행한다
- 반영 결과는 감사로그에 남긴다
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from src.pipeline import store
from src.pipeline.migrations import migrate
from src.privacy import audit

from .base import OPTIONAL_COLUMNS, CpoAdapter


def _upsert(con: sqlite3.Connection, df: pd.DataFrame, adapter: CpoAdapter) -> int:
    out = df.copy()
    out["session_id"] = adapter.operator_id + ":" + out["session_id"].astype(str)
    out["operator_id"] = adapter.operator_id
    out["source"] = adapter.source_kind
    out["start_ts"] = out["start_ts"].astype(str)
    if "end_ts" in out.columns:
        out["end_ts"] = out["end_ts"].astype(str)
    cols = ["session_id", "charger_id", "zone", "start_ts", "duration_h", "energy_kwh", "ev_id", "operator_id", "source"]
    cols += [c for c in OPTIONAL_COLUMNS if c in out.columns]
    exists = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='charger_sessions'").fetchone()
    if not exists:
        out[cols].to_sql("charger_sessions", con, index=False)
        return len(out)
    migrate(con)  # operator_id/source 컬럼 보장
    # 과거 인제스트가 만든 테이블에는 선택 컬럼(ev_id 등)이 없을 수 있다 — 빠진 컬럼은 추가한다
    have = {r[1] for r in con.execute("PRAGMA table_info(charger_sessions)")}
    types = {"zone": "INTEGER", "duration_h": "REAL", "energy_kwh": "REAL", "avg_power_kw": "REAL"}
    for col in cols:
        if col not in have:
            con.execute(f"ALTER TABLE charger_sessions ADD COLUMN {col} {types.get(col, 'TEXT')}")
    ids = out["session_id"].tolist()
    for i in range(0, len(ids), 500):  # SQLite 변수 개수 한도 대비 배치 삭제
        chunk = ids[i:i + 500]
        con.execute(f"DELETE FROM charger_sessions WHERE session_id IN ({','.join('?' * len(chunk))})", chunk)
    out[cols].to_sql("charger_sessions", con, if_exists="append", index=False)
    return len(out)


def sync_operator(adapter: CpoAdapter, con: sqlite3.Connection, since=None) -> dict:
    try:
        df = adapter.fetch_sessions(since)
        n = _upsert(con, df, adapter) if len(df) else 0
        con.commit()
        return {"operator_id": adapter.operator_id, "status": "ok", "rows": n}
    except Exception as exc:  # 사업자 단위 격리
        con.rollback()
        return {"operator_id": adapter.operator_id, "status": "error", "rows": 0, "detail": str(exc)}


def sync_all(adapters: list[CpoAdapter], since=None, con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or store.get_connection()
    try:
        results = [sync_operator(a, con, since) for a in adapters]
    finally:
        if own:
            con.close()
    summary = {"results": results, "ok": all(r["status"] == "ok" for r in results)}
    audit.log("system:cpo_sync", "cpo_sync", detail={
        r["operator_id"]: (r["rows"] if r["status"] == "ok" else "error") for r in results
    })
    return summary
