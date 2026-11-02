"""KPI 산식(src/kpi.py) 테스트 — 계획서 v1.0 정의 고정."""
import numpy as np
import pandas as pd

from src import kpi
from src.datagen.charger import generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry


def _ev(rows):
    return pd.DataFrame(rows, columns=["timestamp", "ev_id", "driving", "connected", "charging_kw", "vehicle_model"])


def test_design_values_match_plan():
    assert kpi.DESIGN_A_SESSIONS == 2520
    assert kpi.DESIGN_B_VEHICLE_DAYS == 3000
    assert kpi.DESIGN_INTERNAL_RECORDS == 5620 >= kpi.TARGET_INTERNAL_RECORDS


def test_vehicle_day_counts_each_active_day_once():
    ev = _ev([
        ("2026-09-01 08:00", "A", True, False, 0.0, "m1"),
        ("2026-09-01 22:00", "A", False, True, 6.0, "m1"),   # 같은 날 → 1건
        ("2026-09-02 03:00", "A", False, False, 0.0, "m1"),  # 비활성 → 제외
        ("2026-09-02 09:00", "B", False, True, 0.0, "m2"),
    ])
    assert kpi.count_b_vehicle_days(ev) == 2


def test_a_sessions_only_linked_to_participant_ev():
    ch = pd.DataFrame({"charger_id": ["c1", "c2", "c3"], "ev_id": ["A", None, "B"]})
    assert kpi.count_a_sessions(ch) == 2
    assert kpi.count_a_sessions(ch.drop(columns="ev_id")) == 0


def test_external_records_zero_for_synthetic_grid():
    ev = _ev([("2026-09-01 08:00", "A", True, False, 0.0, "m1")])
    ch = pd.DataFrame({"charger_id": ["c1"], "ev_id": ["A"]})
    grid = pd.DataFrame({"timestamp": pd.date_range("2026-09-01", periods=24, freq="h")})
    assert kpi.summarize_kpi(ev, ch, grid, grid_is_real=False).external_records == 0
    assert kpi.summarize_kpi(ev, ch, grid, grid_is_real=True).external_records == 96


def test_summary_on_generated_scenario_has_plan_scale():
    cfg = SimConfig(n_ev=30, weeks=21, n_chargers=150, charger_days=147)
    rng = np.random.default_rng(1)
    ev = generate_ev_telemetry(cfg, rng)
    ch = generate_charger_sessions(cfg, rng, ev)
    s = kpi.summarize_kpi(ev, ch)
    assert (s.ev_count, s.charger_count) == (30, 150)
    assert s.internal_records == s.a_sessions + s.b_vehicle_days + s.b_discharge_events
    assert s.b_vehicle_days > 0 and s.a_sessions > 0
