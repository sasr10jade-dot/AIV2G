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
    assert m.migrate(con) == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"charger_registry", "advisories", "vehicle_registrations", "dataset_exports", "external_forecasts",
            "schema_migrations"} <= names
    assert not {"discharge_events", "experiment_groups", "notifications"} & names   # 계획서 범위 밖 테이블은 만들지 않는다
    assert m.current_version(con) == m.LATEST_VERSION


def test_migrate_is_idempotent():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    assert m.migrate(con) == []
    assert con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == m.LATEST_VERSION


def test_existing_sessions_table_keeps_rows_and_gains_plan_columns():
    con = sqlite3.connect(":memory:")
    pd.DataFrame({"session_id": ["S1"], "charger_id": ["C1"], "ev_id": ["A"]}).to_sql("charger_sessions", con, index=False)
    m.migrate(con)
    row = con.execute("SELECT session_id, ev_id, operator_id, charger_type, stop_reason, error_code FROM charger_sessions").fetchone()
    assert row == ("S1", "A", None, None, None, None)


def test_generated_registry_covers_all_chargers_with_three_operators_and_two_types():
    cfg = SimConfig(n_ev=5, weeks=1, n_chargers=10, charger_days=7, n_zones=2)
    rng = np.random.default_rng(3)
    ev = generate_ev_telemetry(cfg, rng)
    ch = generate_charger_sessions(cfg, rng, ev)
    reg = build_charger_registry(cfg, ch)
    assert len(reg) == 10 and set(reg["operator_id"]) == {"CPO-A", "CPO-B", "CPO-C"}
    assert set(reg["charger_type"]) == {"AC_SLOW", "DC_FAST"}
    assert set(ch["operator_id"]) <= {"CPO-A", "CPO-B", "CPO-C"}
    s = kpi.summarize_kpi(ev, ch)
    assert s.operator_count == 3 and s.charger_type_count == 2
    # 계획서 수집 항목: 종료 시각·출력·종료 사유가 채워진다
    assert ch["end_ts"].notna().all() and (ch["end_ts"] > ch["start_ts"]).all()
    assert ch["stop_reason"].notna().all() and (ch["avg_power_kw"] > 0).all()


def test_registration_constraints_and_plate_hash_uniqueness():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    ins = ("INSERT INTO vehicle_registrations (registration_id, username, plate, plate_hash, vehicle_model, occupation, "
           "region_sido, region_sigungu, target_soc_pct, min_soc_pct, status, consent_version, consented_at, created_at) "
           "VALUES (?,?,'43오**50',?,'ioniq5','office','광주광역시','북구',?,?,'pending','profile-v1','t','t')")
    con.execute(ins, ("R1", "u1", "H1", 80, 20))
    with pytest.raises(sqlite3.IntegrityError):          # 같은 번호판(해시)은 대기·승인 중 하나만
        con.execute(ins, ("R2", "u2", "H1", 80, 20))
    with pytest.raises(sqlite3.IntegrityError):          # 목표 배터리 범위 밖
        con.execute(ins, ("R3", "u3", "H3", 150, 20))


def test_external_forecast_primary_key_keeps_batches_apart():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    ins = ("INSERT INTO external_forecasts (batch_id, provider, model_id, model_version, target, scope, timestamp, value, "
           "issued_at, received_at) VALUES (?,?,?,?,?,?,?,?,?,?)")
    row = ("B1", "acryl", "m", "1", "ev_charging_demand_kw", "city", "2027-01-10T00:00:00", 10.0, "t", "t")
    con.execute(ins, row)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(ins, row)
    con.execute(ins, ("B2",) + row[1:])                   # 다른 배치의 같은 시각 예측은 함께 보관(재수신 이력)
