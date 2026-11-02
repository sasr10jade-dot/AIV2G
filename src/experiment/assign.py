"""실험군/대조군 배정 — 어드바이저리·인센티브 효과를 비교하기 위한 그룹 나누기.

- 차종(vehicle_model)별로 층화한다: 각 차종 안에서 해시 순서대로 번갈아 배정해 두 그룹의 차종 구성이
  거의 같아진다(차종 차이가 효과로 오인되는 것을 방지).
- 같은 (experiment_id, 차량 집합)이면 항상 같은 결과다(재현 가능). 이미 배정된 차량은 바꾸지 않는다 —
  실험 도중 그룹이 뒤섞이면 분석이 무효가 되기 때문이다.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from src.pipeline import store

GROUPS = ("control", "treatment")


def _rank_key(experiment_id: str, ev_id: str) -> str:
    return hashlib.sha256(f"{experiment_id}|{ev_id}".encode("utf-8")).hexdigest()


def plan_assignment(experiment_id: str, ev_models: dict[str, str], treatment_ratio: float = 0.5) -> dict[str, str]:
    """{ev_id: group}. 차종별로 해시순 정렬 후 비율에 맞춰 앞에서부터 treatment를 채운다."""
    if not 0 < treatment_ratio < 1:
        raise ValueError("treatment_ratio는 0과 1 사이여야 합니다")
    by_model: dict[str, list[str]] = {}
    for ev_id, model in ev_models.items():
        by_model.setdefault(model or "?", []).append(ev_id)
    out: dict[str, str] = {}
    # 배정 후 남는 소수점이 한 그룹으로 쏠리지 않도록 누적 목표 개수로 판단한다
    placed = treated = 0
    for model in sorted(by_model):
        for ev_id in sorted(by_model[model], key=lambda e: _rank_key(experiment_id, e)):
            placed += 1
            if treated < round(placed * treatment_ratio):
                out[ev_id] = "treatment"
                treated += 1
            else:
                out[ev_id] = "control"
    return out


def assign(experiment_id: str, ev_models: dict[str, str], treatment_ratio: float = 0.5,
           con: sqlite3.Connection | None = None) -> dict[str, str]:
    """신규 차량만 배정해 저장하고, 전체 배정표(기존 포함)를 반환한다."""
    own = con is None
    con = con or store.get_connection()
    try:
        existing = dict(con.execute(
            "SELECT ev_id, group_name FROM experiment_groups WHERE experiment_id=?", (experiment_id,)).fetchall())
        planned = plan_assignment(experiment_id, ev_models, treatment_ratio)
        now = datetime.now(timezone.utc).isoformat()
        for ev_id, grp in planned.items():
            if ev_id not in existing:
                con.execute("INSERT INTO experiment_groups (experiment_id, ev_id, group_name, assigned_at) VALUES (?,?,?,?)",
                            (experiment_id, ev_id, grp, now))
                existing[ev_id] = grp
        con.commit()
        return existing
    finally:
        if own:
            con.close()


def group_of(experiment_id: str, ev_id: str, con: sqlite3.Connection | None = None) -> str | None:
    own = con is None
    con = con or store.get_connection()
    try:
        row = con.execute("SELECT group_name FROM experiment_groups WHERE experiment_id=? AND ev_id=?",
                          (experiment_id, ev_id)).fetchone()
        return row[0] if row else None
    finally:
        if own:
            con.close()
