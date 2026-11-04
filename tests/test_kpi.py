"""KPI 산식(src/kpi.py) 테스트 — 사업수행계획서 v1.0(2026-10-31) 성과지표 고정."""
import numpy as np
import pandas as pd

from src import kpi
from src.datagen.charger import generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry


def _ev(rows):
    return pd.DataFrame(rows, columns=["timestamp", "ev_id", "driving", "connected", "charging_kw", "vehicle_model"])


def _sessions(starts, energy=None):
    return pd.DataFrame({"charger_id": [f"c{i}" for i in range(len(starts))], "start_ts": pd.to_datetime(starts),
                         "energy_kwh": energy if energy is not None else [5.0] * len(starts)})


def test_plan_targets_match_v1_0():
    t = kpi.plan_targets()
    assert (t["ev"], t["ev_models"], t["chargers"], t["charger_types"]) == (30, 2, 150, 2)
    assert t["records_by_year"] == {"Y1": 4000, "Y2": 4500} and t["records_total"] == 8500


def test_project_year_boundaries_and_gap():
    assert kpi.project_year_of("2026-10-31") is None
    assert kpi.project_year_of("2026-11-01") == "Y1"
    assert kpi.project_year_of("2027-04-15 23:00") == "Y1"
    assert kpi.project_year_of("2027-04-20") is None           # 1·2차년도 사이 공백기
    assert kpi.project_year_of("2027-05-01") == "Y2"
    assert kpi.project_year_of("2027-11-30") == "Y2"
    assert kpi.project_year_of("2027-12-01") is None


def test_vehicle_day_counts_each_active_day_once():
    ev = _ev([
        ("2026-12-01 08:00", "A", True, False, 0.0, "m1"),
        ("2026-12-01 22:00", "A", False, True, 6.0, "m1"),   # 같은 날 → 1건
        ("2026-12-02 03:00", "A", False, False, 0.0, "m1"),  # 비활성 → 제외
        ("2026-12-02 09:00", "B", False, True, 0.0, "m2"),
    ])
    assert kpi.count_vehicle_days(ev) == 2


def test_sessions_without_energy_are_not_valid_data():
    ch = _sessions(["2026-12-01", "2026-12-02", "2026-12-03"], energy=[5.0, 0.0, 3.2])
    assert kpi.count_charger_sessions(ch) == 2


def test_records_are_split_by_year_without_overlap():
    """연차별 확보 데이터 미중복 — 각 건은 한 연차에만 들어가고, 공백기·사업기간 밖은 어디에도 넣지 않는다."""
    ev = _ev([
        ("2027-04-15 10:00", "A", True, False, 0.0, "m1"),   # Y1
        ("2027-04-20 10:00", "A", True, False, 0.0, "m1"),   # 공백기
        ("2027-05-02 10:00", "A", True, False, 0.0, "m1"),   # Y2
    ])
    ch = _sessions(["2026-11-05", "2027-03-01", "2027-06-01", "2026-10-01"])
    s = kpi.summarize_kpi(ev, ch)
    y1, y2 = s.by_year
    assert (y1.charger_sessions, y1.vehicle_days, y1.records) == (2, 1, 3)
    assert (y2.charger_sessions, y2.vehicle_days, y2.records) == (1, 1, 2)
    assert s.out_of_period_records == 2
    assert y1.records + y2.records + s.out_of_period_records == s.scenario_records
    assert y1.achieved_pct == round(3 / 4000 * 100, 1)


def test_public_data_is_reference_only_and_zero_when_synthetic():
    ev = _ev([("2026-12-01 08:00", "A", True, False, 0.0, "m1")])
    ch = _sessions(["2026-12-01"])
    grid = pd.DataFrame({"timestamp": pd.date_range("2026-12-01", periods=24, freq="h")})
    assert kpi.summarize_kpi(ev, ch, grid, grid_is_real=False).public_reference_records == 0
    real = kpi.summarize_kpi(ev, ch, grid, grid_is_real=True)
    assert real.public_reference_records == 96
    assert real.by_year[0].records == 2                       # 공개 데이터는 성과 건수에 넣지 않는다


def test_summary_on_generated_scenario_has_plan_scale():
    cfg = SimConfig(n_ev=30, weeks=21, n_chargers=150, charger_days=147, start_date="2026-11-01")
    rng = np.random.default_rng(1)
    ev = generate_ev_telemetry(cfg, rng)
    ch = generate_charger_sessions(cfg, rng, ev)
    s = kpi.summarize_kpi(ev, ch)
    assert (s.ev_count, s.charger_count) == (30, 150)
    assert s.charger_type_count == 2 and s.operator_count == 3   # 완속·급속 2종, 충전서비스 사업자 3사
    assert s.is_real is False                                    # 합성 데이터는 실적이 아니다
    # 1차년도 기간(11/1~4/15) 안의 21주 시나리오만으로도 설계상 4,000건을 넘는다
    assert s.by_year[0].records >= kpi.PROJECT_YEARS["Y1"]["target"]


def test_official_counts_exclude_synthetic_tables():
    """EV만 실측이고 충전기는 합성으로 보완 중이면, 성과 실적에는 차량-일만 들어간다."""
    ev = _ev([("2026-12-01 08:00", "A", True, False, 0.0, "m1"), ("2026-12-02 08:00", "A", True, False, 0.0, "m1")])
    ch = _sessions(["2026-12-01", "2026-12-02", "2026-12-03"])
    prov = {"ev_telemetry": "measured", "charger_sessions": "synthetic", "grid_signal": "synthetic"}
    s = kpi.summarize_kpi(ev, ch, provenance=prov)
    assert s.by_year[0].records == 5 and s.official_by_year[0].records == 2
    assert (s.official_by_year[0].charger_sessions, s.official_by_year[0].vehicle_days) == (0, 2)
    assert s.is_real is True and s.provenance == prov
    demo = kpi.summarize_kpi(ev, ch)
    assert demo.official_by_year[0].records == 0 and demo.is_real is False


def test_infra_status_counts_sites_operators_and_active_vehicles():
    from src.kpi import infra_status
    ch = pd.DataFrame({
        "charger_id": ["C1", "C2", "C3", "C3"], "operator_id": ["A", "B", "A", "A"],
        "site_id": ["S1", "S2", "S3", "S3"], "charger_type": ["AC_SLOW", "DC_FAST", "AC_SLOW", "AC_SLOW"],
        "energy_kwh": [5.0, 0.0, 7.0, 3.0],
    })
    ts = pd.to_datetime(["2026-11-01 08:00", "2026-11-20 08:00", "2026-11-14 09:00"])
    ev = pd.DataFrame({"timestamp": ts, "ev_id": ["E1", "E2", "E3"], "vehicle_model": ["m1", "m2", "m1"],
                       "driving": [True, True, False], "connected": [False, False, True], "charging_kw": [0.0, 0.0, 0.0]})
    out = infra_status(ev, ch)
    assert out["site_count"] == 3 and out["targets"]["sites"] == 3
    a = next(o for o in out["operators"] if o["operator_id"] == "A")
    assert a["chargers"] == 2 and a["sessions"] == 3 and a["charger_types"] == {"AC_SLOW": 2}
    v = out["vehicles"]
    assert v["total"] == 3 and v["by_model"] == {"m1": 2, "m2": 1}
    assert v["as_of"] == "2026-11-20" and v["active"] == 2          # 11/14~11/20 안에 관측된 E2·E3


def test_infra_status_without_site_column_is_not_counted():
    from src.kpi import infra_status
    out = infra_status(pd.DataFrame(), pd.DataFrame({"charger_id": ["C1"], "energy_kwh": [1.0]}))
    assert out["site_count"] is None and out["sites"] == [] and out["vehicles"]["total"] == 0


def test_collection_status_projects_year_end_and_warns():
    from src.kpi import collection_status
    days = pd.date_range("2026-12-01", "2026-12-14", freq="D")
    ch = pd.DataFrame({"start_ts": days, "energy_kwh": 5.0, "charger_id": "C1"})            # 하루 1건
    ev = pd.DataFrame({"timestamp": days, "ev_id": "E1", "vehicle_model": "m", "driving": True,
                       "connected": False, "charging_kw": 0.0, "soc_pct": 50.0})               # 하루 1건
    out = collection_status(ev, ch, days=14)
    p = out["pace"]
    assert p["year"] == "Y1" and p["per_day"] == 2.0 and p["counted"] == 28
    assert p["remaining_days"] == (pd.Timestamp("2027-04-15") - pd.Timestamp("2026-12-14")).days
    assert p["expected"] == 28 + 2 * p["remaining_days"] and p["expected"] < 4000
    codes = {w["code"]: w for w in out["warnings"]}
    assert codes["pace_short"]["expected"] == p["expected"] and codes["few_active"] == {"code": "few_active", "active": 1, "target": 30}
    assert len(out["daily"]) == 14 and out["daily"][-1] == {"date": "2026-12-14", "charger_sessions": 1, "vehicle_days": 1}
    r = out["roster"][0]
    assert r["ev_id"] == "E1" and r["active"] is True and r["last_soc_pct"] == 50.0


def test_collection_status_excludes_synthetic_tables():
    from src.kpi import collection_status
    ch = pd.DataFrame({"start_ts": pd.to_datetime(["2026-12-01"]), "energy_kwh": [5.0], "charger_id": ["C1"]})
    out = collection_status(pd.DataFrame(), ch, count_sessions=False)
    assert out["daily"] == [] and out["pace"] is None
