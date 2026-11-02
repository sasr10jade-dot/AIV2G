"""FastAPI 라우터(ingest/forecast/optimize/models/dashboard) 통합 테스트.

지금까지 curl로 수동 스모크테스트했던 흐름(데이터 생성/업로드 -> 예측 -> 최적화 ->
대시보드)을 회귀테스트로 고정한다. RAW_DIR(data/raw)은 프로젝트 공용 경로라 매
테스트 전후로 비워 상태를 격리한다 — 비어 있으면 load_scenario가 합성 데모 데이터를
즉석 생성하므로 다른 테스트에 영향이 없다.
"""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.automation.model_registry import ModelRegistry
from src.automation.scheduler import bootstrap
from src.datagen.charger import generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.pipeline import source

client = TestClient(app)

# 레지스트리 키(target_col)는 city_hourly의 실제 컬럼명과 같아야 하므로, 두 예측
# 대상(ev_charging_demand_kw/ev_available_v2g_kwh) 중 하나를 그대로 쓴다. 이미
# 등록돼 있으면 그대로 쓰고(변경 없음), 없으면 작은 cfg로 처음 등록한다 — 어느
# 쪽이든 실제 서비스에 쓰이는 레지스트리이므로 테스트가 끝나도 지우지 않는다.
FORECAST_TARGET = "ev_available_v2g_kwh"


@pytest.fixture(autouse=True)
def _logged_in_admin(tmp_path, monkeypatch):
    """대부분의 라우터가 로그인(admin 이상)을 요구하므로, 실제 계정 파일과 무관한
    임시 계정으로 매 테스트마다 로그인해둔다."""
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("test-admin", "test-password-1", "admin")
    client.post("/auth/login", json={"username": "test-admin", "password": "test-password-1"})
    yield
    client.post("/auth/logout")


@pytest.fixture(autouse=True)
def _clean_raw_data():
    source.clear_raw_data()
    yield
    source.clear_raw_data()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_root_redirects_to_dashboard():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/dashboard"


def test_models_list_includes_known_targets():
    r = client.get("/models")
    assert r.status_code == 200
    body = r.json()
    assert "ev_charging_demand_kw" in body
    assert "ev_available_v2g_kwh" in body


def test_models_detail_404_for_unregistered_target():
    r = client.get("/models/definitely_not_registered_xyz")
    assert r.status_code == 404


def test_forecast_404_for_unregistered_target():
    r = client.get("/forecast/definitely_not_registered_xyz")
    assert r.status_code == 404


def test_plugin_pattern_unknown_vehicle_returns_404():
    r = client.get("/forecast/plugin-pattern/NO-SUCH-EV")
    assert r.status_code == 404


def test_plugin_pattern_demo_scenario_returns_probability_curve():
    """기본 데모 시나리오(SimConfig 기본값 16주)는 패턴 분석에 필요한 최소 이력을
    이미 만족하므로, 데모 EV 중 하나로 바로 조회할 수 있어야 한다."""
    cfg_r = client.get("/dashboard/data")
    assert cfg_r.status_code == 200
    ev_count = cfg_r.json()["kpi"]["ev_count"]
    assert ev_count > 0
    r = client.get("/forecast/plugin-pattern/EV-001", params={"horizon_hours": 24})
    assert r.status_code == 200
    body = r.json()
    assert body["ev_id"] == "EV-001"
    assert len(body["timestamps"]) == 24
    assert len(body["connection_probability"]) == 24
    assert body["is_real_data"] is False


def test_plugin_pattern_rejects_out_of_range_horizon():
    assert client.get("/forecast/plugin-pattern/EV-001", params={"horizon_hours": 0}).status_code == 422
    assert client.get("/forecast/plugin-pattern/EV-001", params={"horizon_hours": 999}).status_code == 422


class TestForecastWithRegisteredModel:
    @classmethod
    def setup_class(cls):
        registry = ModelRegistry(FORECAST_TARGET)
        if registry.active_version() is None:
            cfg = SimConfig(n_ev=5, weeks=5, n_chargers=5, charger_days=35, n_zones=2)
            bootstrap(registry, cfg, FORECAST_TARGET)

    def test_forecast_returns_predictions(self):
        r = client.get(f"/forecast/{FORECAST_TARGET}", params={"periods": 5})
        assert r.status_code == 200
        body = r.json()
        assert body["target_col"] == FORECAST_TARGET
        assert body["is_real_data"] is False
        assert len(body["y_pred"]) == 5
        assert len(body["y_actual"]) == 5
        assert all(v >= 0 for v in body["y_pred"])


def test_optimize_poc_compare_returns_peak_reduction():
    r = client.post("/optimize/poc-compare", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["is_real_data"] is False
    assert body["baseline_peak_kw"] >= body["optimized_peak_kw"]
    assert 0 <= body["peak_reduction_pct"] <= 100


def test_dashboard_page_serves_html():
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "AI-V2G" in r.text


def test_dashboard_data_reflects_scenario_params():
    r = client.get("/dashboard/data", params={"n_ev": 6, "n_chargers": 8, "weeks": 4})
    assert r.status_code == 200
    body = r.json()
    assert body["scenario"]["n_ev"] == 6
    assert body["scenario"]["weeks"] == 4
    assert body["is_real_data"] is False


def test_dashboard_data_includes_renewable_summary():
    r = client.get("/dashboard/data", params={"n_ev": 6, "n_chargers": 8, "weeks": 4})
    assert r.status_code == 200
    renewable = r.json()["renewable"]
    assert set(renewable.keys()) == {"hourly", "daily", "weekly"}
    for view in renewable.values():
        assert len(view["timestamps"]) == len(view["power_demand_mw"]) == len(view["renewable_gen_mw"])
        assert view["avg_demand_mw"] > 0
        assert 0 <= view["renewable_share_pct"] <= 100


def test_charging_efficiency_demo_scenario_recovers_efficiency():
    """데모(합성) 시나리오는 charger.py가 CHARGER_EFFICIENCY로 나눈 값을 쓰므로,
    ev_id가 연결된 세션이 존재하고 평균 효율이 합리적 범위로 나와야 한다."""
    r = client.get("/dashboard/charging-efficiency", params={"n_ev": 10, "weeks": 6, "n_chargers": 10})
    assert r.status_code == 200
    body = r.json()
    assert body["n_sessions_analyzed"] > 0
    assert 50.0 < body["avg_efficiency_pct"] <= 150.0
    assert body["is_real_data"] is False


def test_dashboard_data_caches_heavy_computation():
    """LP 최적화·Closed-loop 검증은 4초 가까이 걸려(실측) 화면을 열 때마다 다시
    계산하면 서버가 응답 불능처럼 보이는 원인이 됐다(라이브 서버에서 재현됨) —
    같은 시나리오·최적화 파라미터로 반복 호출하면 캐시를 써서 한 번만 계산돼야 한다."""
    from src.api.routers import dashboard as dashboard_module
    dashboard_module._heavy_compute_cache.clear()

    call_count = {"n": 0}
    original = dashboard_module.run_closed_loop_cycle

    def counting_wrapper(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    import unittest.mock
    with unittest.mock.patch.object(dashboard_module, "run_closed_loop_cycle", counting_wrapper):
        params = {"n_ev": 5, "n_chargers": 5, "weeks": 4}
        r1 = client.get("/dashboard/data", params=params)
        r2 = client.get("/dashboard/data", params=params)
        assert r1.status_code == 200 and r2.status_code == 200
        assert call_count["n"] == 1

    dashboard_module._heavy_compute_cache.clear()


def test_dashboard_data_cache_invalidated_by_different_scenario():
    from src.api.routers import dashboard as dashboard_module
    dashboard_module._heavy_compute_cache.clear()

    call_count = {"n": 0}
    original = dashboard_module.run_closed_loop_cycle

    def counting_wrapper(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    import unittest.mock
    with unittest.mock.patch.object(dashboard_module, "run_closed_loop_cycle", counting_wrapper):
        client.get("/dashboard/data", params={"n_ev": 5, "n_chargers": 5, "weeks": 4})
        client.get("/dashboard/data", params={"n_ev": 6, "n_chargers": 5, "weeks": 4})
        assert call_count["n"] == 2, "시나리오 규모가 다르면 캐시를 재사용하면 안 된다"

    dashboard_module._heavy_compute_cache.clear()


def test_dashboard_data_rejects_out_of_range_params():
    r = client.get("/dashboard/data", params={"n_ev": 999999})
    assert r.status_code == 422


def test_dashboard_data_includes_profit_sharing_with_default_shares():
    r = client.get("/dashboard/data", params={"n_ev": 6, "n_chargers": 8, "weeks": 4})
    assert r.status_code == 200
    ps = r.json()["profit_sharing"]
    assert ps["shares_pct"] == {"owner": 50.0, "operator": 30.0, "platform": 20.0}
    assert set(ps["amounts_krw"]) == {"owner", "operator", "platform"}


def test_dashboard_data_rejects_profit_sharing_shares_not_summing_to_100():
    r = client.get(
        "/dashboard/data",
        params={"n_ev": 6, "n_chargers": 8, "weeks": 4, "owner_share_pct": 50, "operator_share_pct": 40, "platform_share_pct": 20},
    )
    assert r.status_code == 422


def test_copilot_ask_without_api_key_returns_503(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.post("/copilot/ask", json={"question": "이 프로젝트의 1차년도 목표는?"})
    assert r.status_code == 503


def test_copilot_ask_with_ev_id_computes_vehicle_plan_before_key_check(monkeypatch):
    """ev_id가 있으면 실제 LP 최적화(run_v2g_optimization)를 먼저 계산해 컨텍스트에
    얹는데, 이 계산 자체는 GEMINI_API_KEY 없이도 동작해야 하고(순수 최적화 로직),
    최종적으로는 여전히 Gemini 호출 단계에서 503이 나야 한다 — 새 vehicle_plan 경로가
    기존 503 동작을 깨지 않는지 확인."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.post("/copilot/ask", json={"question": "내 차 언제 방전하면 얼마 벌어?", "ev_id": "EV-001"})
    assert r.status_code == 503


def test_copilot_ask_with_unknown_ev_id_does_not_crash(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.post("/copilot/ask", json={"question": "테스트", "ev_id": "NO-SUCH-EV"})
    assert r.status_code == 503


def test_copilot_ask_uses_owned_ev_id_when_ev_id_omitted(monkeypatch):
    """ev_id를 안 주면 로그인 계정의 owned_ev_id를 자동으로 써야 한다 — 이것도
    vehicle_status/vehicle_plan 계산 경로를 타므로 503(Gemini 키 없음) 전에
    크래시 없이 도달해야 한다."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    client.patch("/auth/me/owned-vehicle", json={"owned_ev_id": "EV-001"})
    try:
        r = client.post("/copilot/ask", json={"question": "내 차 지금 SOC 몇이야?"})
        assert r.status_code == 503
    finally:
        client.patch("/auth/me/owned-vehicle", json={"owned_ev_id": None})


def test_copilot_ask_accepts_history_without_crashing(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.post("/copilot/ask", json={
        "question": "그럼 얼마야?",
        "history": [{"role": "user", "text": "내 차 방전하면 얼마 벌어?"}, {"role": "assistant", "text": "약 3,000원입니다."}],
    })
    assert r.status_code == 503


def test_copilot_ask_includes_stat_summary_from_run_history(monkeypatch):
    """cfg(n_ev/weeks 기본값)와 일치하는 과거 실행 이력이 있으면, 그걸로 만든
    통계 요약본이 ask()에 전달되고 응답에도 함께 실려야 한다."""
    from src.api.routers import copilot as copilot_module

    fake_history = pd.DataFrame([{
        "generated_at": "2026-09-01T00:00:00", "n_ev": 30, "weeks": 21,
        "savings_krw": 1000.0, "savings_pct": 20.0, "forecast_mae": 1.5,
        "closed_loop_drift_pct": 5.0, "closed_loop_improve_pct": 10.0,
    }])
    monkeypatch.setattr(copilot_module, "read_dashboard_run_history", lambda limit=20: fake_history)
    captured = {}

    def fake_ask(question, dashboard_snapshot, vehicle_plan, next_session_plan, vehicle_status, history, stat_summary):
        captured["stat_summary"] = stat_summary
        return "테스트 답변"

    monkeypatch.setattr(copilot_module, "ask", fake_ask)
    r = client.post("/copilot/ask", json={"question": "지난번보다 나아졌어?"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "테스트 답변"
    assert body["stat_summary"] == captured["stat_summary"]
    assert "20.0%" in body["stat_summary"]


def test_copilot_ask_omits_stat_summary_when_no_history(monkeypatch):
    from src.api.routers import copilot as copilot_module

    monkeypatch.setattr(copilot_module, "read_dashboard_run_history", lambda limit=20: pd.DataFrame())
    monkeypatch.setattr(copilot_module, "ask", lambda *a, **k: "테스트 답변")
    r = client.post("/copilot/ask", json={"question": "아무 질문"})
    assert r.status_code == 200
    assert r.json()["stat_summary"] is None


def test_sync_kpx_smp_without_service_key_returns_503(monkeypatch):
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    r = client.post("/ingest/sync-kpx-smp")
    assert r.status_code == 503


def test_explain_schedule_without_api_key_returns_503(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.get("/copilot/explain-schedule")
    assert r.status_code == 503


def test_explain_schedule_unknown_ev_returns_404(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-called")
    r = client.get("/copilot/explain-schedule", params={"ev_id": "NO-SUCH-EV"})
    assert r.status_code == 404


def test_explain_next_session_unknown_ev_returns_404(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-called")
    r = client.get("/copilot/explain-next-session", params={"ev_id": "NO-SUCH-EV"})
    assert r.status_code == 404


def test_explain_next_session_without_api_key_returns_503(monkeypatch):
    """알려진 차량(EV-001, 기본 데모 시나리오는 16주라 이력이 충분함)이면 계획
    계산까지는 성공하고, Gemini 호출 단계에서만 503이 나야 한다."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = client.get("/copilot/explain-next-session", params={"ev_id": "EV-001"})
    assert r.status_code == 503


class TestIngestUploadFlow:
    def test_upload_valid_data_switches_to_real_mode(self):
        cfg = SimConfig(n_ev=3, weeks=2, n_chargers=3, charger_days=14, n_zones=1)
        rng = np.random.default_rng(cfg.random_seed)
        ev_df = generate_ev_telemetry(cfg, rng)
        charger_df = generate_charger_sessions(cfg, rng)
        grid_df = generate_grid_signal(cfg, rng)

        files = {
            "ev_telemetry": ("ev.csv", ev_df.to_csv(index=False).encode(), "text/csv"),
            "charger_sessions": ("charger.csv", charger_df.to_csv(index=False).encode(), "text/csv"),
            "grid_signal": ("grid.csv", grid_df.to_csv(index=False).encode(), "text/csv"),
        }
        r = client.post("/ingest/upload", files=files)
        assert r.status_code == 200
        assert r.json()["mode"] == "uploaded"
        assert client.get("/ingest/status").json() == {"mode": "uploaded", "has_raw_data": True}

    def test_upload_invalid_data_is_rejected(self):
        bad_ev = pd.DataFrame(
            {
                "timestamp": pd.date_range("2026-01-01", periods=3, freq="h"),
                "ev_id": ["EV-1"] * 3,
                "ev_type": ["x"] * 3,
                "zone": [0, 0, 0],
                "soc_pct": [50.0, 999.0, -5.0],  # 2건 범위 위반
                "connected": [True] * 3,
                "driving": [False] * 3,
                "charging_kw": [1.0] * 3,
            }
        )
        empty_charger = "session_id,charger_id,zone,start_ts,duration_h,energy_kwh\n"
        empty_grid = "timestamp,power_demand_mw,renewable_gen_mw,temperature_c,price_krw_per_kwh,is_holiday\n"

        files = {
            "ev_telemetry": ("ev.csv", bad_ev.to_csv(index=False).encode(), "text/csv"),
            "charger_sessions": ("charger.csv", empty_charger.encode(), "text/csv"),
            "grid_signal": ("grid.csv", empty_grid.encode(), "text/csv"),
        }
        r = client.post("/ingest/upload", files=files)
        assert r.status_code == 422
        assert client.get("/ingest/status").json()["mode"] == "demo"

    def test_reset_returns_to_demo_mode(self):
        assert client.delete("/ingest/upload").json() == {"status": "ok", "mode": "demo"}
        assert client.get("/ingest/status").json() == {"mode": "demo", "has_raw_data": False}

    def test_obd_emulator_run_populates_real_ev_data(self):
        r = client.post("/ingest/obd-emulator/run", params={"n_vehicles": 3, "n_days": 2, "seed": 7})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "uploaded"
        assert body["n_vehicles"] == 3
        assert body["total_ev_rows"] > 0
        assert len(body["file_reports"]) == 3 * 2
        assert client.get("/ingest/status").json()["mode"] == "uploaded"

    def test_obd_emulator_run_rejects_out_of_range_params(self):
        assert client.post("/ingest/obd-emulator/run", params={"n_vehicles": 0}).status_code == 422
        assert client.post("/ingest/obd-emulator/run", params={"n_days": 999}).status_code == 422


class TestGridDrApi:
    def setup_method(self):
        from src.grid import dr_events
        dr_events.clear_dr_events()

    def teardown_method(self):
        self.setup_method()

    def test_create_list_and_delete_dr_event(self):
        r = client.post("/grid/dr-events", json={
            "start_ts": "2026-09-18T18:00:00", "end_ts": "2026-09-18T20:00:00",
            "severity": "high", "reason": "테스트",
        })
        assert r.status_code == 200
        event_id = r.json()["event_id"]

        r = client.get("/grid/dr-events")
        assert r.status_code == 200
        assert any(e["event_id"] == event_id for e in r.json())

        r = client.delete(f"/grid/dr-events/{event_id}")
        assert r.status_code == 200
        assert client.get("/grid/dr-events").json() == []

    def test_create_dr_event_rejects_invalid_severity(self):
        r = client.post("/grid/dr-events", json={
            "start_ts": "2026-09-18T18:00:00", "end_ts": "2026-09-18T20:00:00", "severity": "extreme",
        })
        assert r.status_code == 422

    def test_delete_unknown_dr_event_returns_404(self):
        assert client.delete("/grid/dr-events/DR-nonexistent").status_code == 404

    def test_dr_performance_reports_overlap_and_non_overlap(self):
        # 기본 데모 시나리오(n_ev=40, weeks=16)는 관측기간이 "어제 23시"에 끝나도록
        # 생성된다(SimConfig.start_date 관례) — "지금"은 그 범위 밖이라, 확실히
        # 범위 안에 들어가는 "3일 전"을 겹치는 이벤트 시각으로 쓴다.
        anchor = pd.Timestamp.now().normalize() - pd.Timedelta(days=3)
        r = client.post("/grid/dr-events", json={
            "start_ts": (anchor - pd.Timedelta(hours=1)).isoformat(),
            "end_ts": (anchor + pd.Timedelta(hours=1)).isoformat(),
            "severity": "high", "reason": "겹치는 이벤트",
        })
        overlapping_id = r.json()["event_id"]
        r = client.post("/grid/dr-events", json={
            "start_ts": "2000-01-01T00:00:00", "end_ts": "2000-01-01T02:00:00",
            "severity": "low", "reason": "안 겹치는 이벤트",
        })
        far_past_id = r.json()["event_id"]

        r = client.get("/dashboard/dr-performance")
        assert r.status_code == 200
        by_id = {e["event_id"]: e for e in r.json()}
        assert by_id[overlapping_id]["overlapped"] is True
        assert by_id[far_past_id]["overlapped"] is False
        assert by_id[far_past_id]["baseline_kwh"] == 0.0


def test_dashboard_kpi_exposes_plan_targets_breakdown_and_degradation_assumption():
    kpi = client.get("/dashboard/data").json()
    k = kpi["kpi"]
    assert k["targets"]["records_by_year"] == {"Y1": 4000, "Y2": 4500}
    assert (k["targets"]["ev"], k["targets"]["chargers"]) == (30, 150)
    assert [y["year"] for y in k["data_by_year"]] == ["Y1", "Y2"]
    assert sum(y["records"] for y in k["data_by_year"]) + k["data_out_of_period"] == k["data_scenario_records"]
    assert k["data_is_real"] is False and "discharge_events" not in k["targets"]
    rev = kpi["revenue_comparison"]
    assert rev["degradation_cost_krw"] >= 0 and rev["assumptions"]["verified"] is False
    assert rev["savings_krw"] == pytest.approx(rev["savings_before_degradation_krw"] - rev["degradation_cost_krw"], abs=1)


def test_root_redirect_is_dashboard_by_default_and_configurable(monkeypatch):
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    monkeypatch.setenv("ROOT_REDIRECT", "/dev")
    assert client.get("/", follow_redirects=False).headers["location"] == "/dev"
    monkeypatch.setenv("ROOT_REDIRECT", "//evil.example")          # 외부 주소로는 보내지 않는다
    assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
