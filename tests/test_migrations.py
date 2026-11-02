"""curated.db 마이그레이션 테스트 — 멱등성·기존 데이터 보존·제약."""
import sqlite3

import numpy as np
import pandas as pd
import pytest

from src import kpi
from src.datagen.charger import build_charger_registry, generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.pipeline import migrations as m


def test_fresh_db_gets_all_tables_and_version():
    con = sqlite3.connect(":memory:")
    assert m.migrate(con) == [1, 2, 3, 4, 5, 6]
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"charger_registry", "discharge_events", "schema_migrations"} <= names
    assert m.current_version(con) == m.LATEST_VERSION


def test_migrate_is_idempotent():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    assert m.migrate(con) == []
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == m.LATEST_VERSION


def test_existing_sessions_table_keeps_rows_and_gains_columns():
    con = sqlite3.connect(":memory:")
    pd.DataFrame({"session_id": ["S1"], "charger_id": ["C1"], "ev_id": ["A"]}).to_sql("charger_sessions", con, index=False)
    m.migrate(con)
    row = con.execute("SELECT session_id, ev_id, operator_id, source FROM charger_sessions").fetchone()
    assert row == ("S1", "A", None, None)


def test_discharge_kind_constraint_and_kpi_counts_only_real_events():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    ins = "INSERT INTO discharge_events VALUES (?,?,?,?,?,?,?,?)"
    con.execute(ins, ("E1", "A", None, "2026-09-01T20:00", 1.0, 3.0, "V2G", "ocpp"))
    con.execute(ins, ("E2", "A", None, "2026-09-02T20:00", 1.0, 3.0, "TEST", "bench"))
    con.execute(ins, ("E3", "B", None, "2026-09-02T21:00", 0.5, 2.0, "V2L", "obd"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, ("E4", "B", None, "2026-09-03", 1.0, 1.0, "BAD", None))
    assert kpi.count_discharge_events(con) == 2


def test_generated_registry_covers_all_chargers_with_two_operators():
    cfg = SimConfig(n_ev=5, weeks=1, n_chargers=10, charger_days=7, n_zones=2)
    rng = np.random.default_rng(3)
    ev = generate_ev_telemetry(cfg, rng)
    ch = generate_charger_sessions(cfg, rng, ev)
    reg = build_charger_registry(cfg, ch)
    assert len(reg) == 10 and set(reg["operator_id"]) == {"CPO-A", "CPO-B"}
    assert set(ch["operator_id"]) <= {"CPO-A", "CPO-B"}
    assert kpi.summarize_kpi(ev, ch).operator_count == 2


def test_v6_converts_plaintext_plates_from_v5_to_encrypted_columns():
    """v5까지만 적용된 DB에 평문 번호판이 있으면 v6이 암호화하고 평문을 지운다."""
    from src.privacy import vault

    con = sqlite3.connect(":memory:")
    for version, name, fn in m.MIGRATIONS[:5]:
        con.execute(f"CREATE TABLE IF NOT EXISTS {m.MIGRATIONS_TABLE} (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
        fn(con)
        con.execute(f"INSERT INTO {m.MIGRATIONS_TABLE} VALUES (?,?,?)", (version, name, "t"))
    con.execute(
        "INSERT INTO vehicle_registrations (registration_id, username, plate, vehicle_model, occupation, region_sido, "
        "region_sigungu, status, consent_version, consented_at, created_at) "
        "VALUES ('R1','u1','43오3450','ioniq5','office','광주광역시','북구','pending','profile-v1','t','t')")
    con.commit()
    assert m.migrate(con) == [6]
    plate, enc, h = con.execute("SELECT plate, plate_enc, plate_hash FROM vehicle_registrations").fetchone()
    assert plate == "43오**50" and "3450" not in plate
    assert vault.decrypt(enc) == "43오3450" and h == vault.blind_index("43오3450")
    # 같은 번호판은 해시 유니크 인덱스로 막힌다
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO vehicle_registrations (registration_id, username, plate, vehicle_model, occupation, region_sido, "
            "region_sigungu, status, consent_version, consented_at, created_at, plate_hash) "
            "VALUES ('R2','u2','43오**50','ioniq5','office','광주광역시','북구','pending','profile-v1','t','t',?)", (h,))
