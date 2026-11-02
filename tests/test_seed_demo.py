"""에뮬레이션 데모 시드 테스트 — 멱등, 공식 실적과 분리, reset으로 가상 행만 제거."""
import importlib.util
from pathlib import Path

from src import kpi
from src.pipeline import store
from src.privacy import consent

SPEC = importlib.util.spec_from_file_location("seed_demo", Path(__file__).resolve().parents[1] / "scripts" / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed_demo)


def test_seed_is_idempotent_separates_emulated_and_reset_removes_only_virtual_rows():
    con = store.get_connection()
    try:
        first = seed_demo.seed(con)
        assert first["charger_registry"] == 150 and set(first["cpo_sync"]) == {"CPO-A", "CPO-B"}
        assert first["discharge_events_new"] > 0 and first["advisories_new"] == 12
        assert sum(first["experiment"].values()) == 30

        again = seed_demo.seed(con)
        assert again["discharge_events_new"] == 0 and again["advisories_new"] == 0

        # 에뮬레이션 방전은 공식 실적에 섞이지 않는다
        assert kpi.count_discharge_events(con) == 0
        assert kpi.count_discharge_events(con, emulated=True) == first["discharge_events_new"]
        assert consent.has_consent("EV-001", "incentive")

        # 실제 기록은 reset 후에도 남는다
        con.execute("INSERT INTO discharge_events VALUES ('REAL1','EV-001',NULL,'2026-09-01T20:00',1,3,'V2G','ocpp')")
        con.commit()
        out = seed_demo.reset(con)
        assert out["discharge_events"] == first["discharge_events_new"] and out["consents"] > 0
        assert kpi.count_discharge_events(con) == 1 and kpi.count_discharge_events(con, emulated=True) == 0
        assert not consent.has_consent("EV-001", "incentive")
    finally:
        con.close()
