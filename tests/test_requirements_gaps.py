"""요구사항정의서 v1.0 미흡·누락 보강 항목 테스트 — K01 집계 단위, A02 제외 차종, Q04 위치정보 동의, A07 관용/민간,
K02 예비 Pool, A04 CPO 수신 실적 분리, C04·Y2A02 조건별 분석 축, 내부망 간편 로그인, E01 한계 시트."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import kpi
from src.api import app
from src.auth import store as auth_store
from src.registry import options, service

client = TestClient(app)


@pytest.fixture(autouse=True)
def users(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    yield
    client.post("/auth/logout")


def _admin():
    auth_store.create_user("admin-gap", "admin-pass-1", "admin")
    assert client.post("/auth/login", json={"username": "admin-gap", "password": "admin-pass-1"}).status_code == 200


# ------------------------------------------------------------------ K01
def test_vehicle_records_are_driving_or_charging_sessions():
    ts = pd.to_datetime(["2026-12-01 07:00", "2026-12-01 08:00", "2026-12-01 09:00", "2026-12-01 18:00",
                         "2026-12-01 22:00", "2026-12-01 23:00", "2026-12-02 03:00"])
    ev = pd.DataFrame({"timestamp": ts, "ev_id": "A",
                       "driving": [True, True, False, True, False, False, False],
                       "connected": [False, False, True, False, True, True, True],
                       "charging_kw": [0, 0, 0, 0, 7.0, 7.0, 0]})
    # 출근 운행(07~08시) 1 + 퇴근 운행(18시) 1 + 충전(22~23시) 1 = 3세션 / 같은 날 하루 = 차량-일 1
    assert kpi.count_vehicle_sessions(ev) == 3
    assert kpi.count_vehicle_days(ev) == 2       # 12/2 03시 연결은 활동으로 보던 옛 단위
    s = kpi.summarize_kpi(ev, pd.DataFrame({"charger_id": ["c"], "start_ts": pd.to_datetime(["2026-12-01"]), "energy_kwh": [5.0]}))
    assert s.by_year[0].vehicle_days == 3 and s.by_year[0].records == 4


# ------------------------------------------------------------------ A02
def test_jeju_v2g_models_are_excluded_from_signup():
    cat = options.load_catalog()
    ids = {m["id"] for m in cat.public_list()}
    assert "ev9" not in ids and "ioniq9" not in ids and "ioniq5" in ids
    assert {m["id"] for m in cat.excluded_list()} == {"ev9", "ioniq9"}
    with pytest.raises(service.RegistryError, match="제주"):
        service.validate_profile("12가3456", "ev9", "office", "광주광역시", "북구")


# ------------------------------------------------------------------ Q04
def test_location_consent_purpose_and_owner_toggle():
    from src.privacy import consent
    assert "location" in consent.PURPOSES
    auth_store.create_user("owner-loc", "owner-pass-1", "owner", owned_ev_id="EV-LOC-1")
    assert client.post("/auth/login", json={"username": "owner-loc", "password": "owner-pass-1"}).status_code == 200
    g = client.get("/owner/consent/location").json()
    assert g["location"] is False and "위치정보" in g["text"]
    assert client.put("/owner/consent/location", json={"granted": True}).json() == {"location": True}
    assert client.put("/owner/consent/location", json={"granted": False}).json() == {"location": False}


# ------------------------------------------------------------------ A07 · K02
def test_affiliation_and_reserve_status():
    rid = service.submit("aff-user", "12가3456", "ioniq5", "office", "광주광역시", "북구", options.CONSENT_VERSION)
    reg = service.list_all()[0]
    assert reg["affiliation"] == "private" and service.view(reg)["affiliation_label"] == "민간"
    service.set_affiliation(rid, "public")
    assert service.list_all()[0]["affiliation"] == "public"
    with pytest.raises(service.RegistryError):
        service.set_affiliation(rid, "military")
    st = service.reserve_status()
    assert st["reserve_count"] == 0 and st["level"] == "warning" and (st["target_min"], st["target_max"]) == (6, 9)


def test_replace_inactive_requires_reserve_and_records_history():
    auth_store.create_user("res-user", "owner-pass-1", "owner", allow_unlinked_owner=True)
    rid = service.submit("res-user", "34나5678", "ev6", "office", "광주광역시", "북구", options.CONSENT_VERSION)
    with pytest.raises(service.RegistryError, match="예비"):
        service.replace_inactive("EV-OLD-1", rid, "admin", "장기 미운행")
    service.set_reserve(rid, "admin", True, "대체용")
    out = service.replace_inactive("EV-OLD-1", rid, "admin", "장기 미운행")
    assert out["new_ev_id"] and service.replacement_history()[0]["inactive_ev_id"] == "EV-OLD-1"


# ------------------------------------------------------------------ A04
def test_cpo_ingest_writes_runtime_status_not_config(tmp_path):
    from src.api.routers import cpo
    before = cpo.CPO_CONFIG_PATH.read_bytes()
    payload = {"batch_date": "2026-11-05", "sessions": [{"session_id": "T1", "charger_id": "C1", "start_ts": "2026-11-05T01:00:00", "energy_kwh": 3.0}]}
    assert client.post("/cpo/ingest/niceinfra", json=payload).status_code == 200
    assert cpo.CPO_CONFIG_PATH.read_bytes() == before                     # 버전관리되는 설정 파일은 그대로
    assert cpo.load_cpo_status()["niceinfra"]["cumulative_sessions"] == 1
    assert cpo.load_cpo_status()["niceinfra"]["authenticated"] is False   # 토큰 미설정(협약 전)은 미인증으로 기록


def test_cpo_ingest_requires_token_when_configured(monkeypatch):
    monkeypatch.setenv("CPO_NICEINFRA_TOKEN", "secret-token-123")
    payload = {"batch_date": "2026-11-05", "sessions": []}
    assert client.post("/cpo/ingest/niceinfra", json=payload).status_code == 401
    assert client.post("/cpo/ingest/niceinfra", json=payload, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/cpo/ingest/niceinfra", json=payload, headers={"Authorization": "Bearer secret-token-123"}).status_code == 200


# ------------------------------------------------------------------ 내부망 간편 로그인
def test_quick_login_lan_only_blocks_proxied_and_public_requests(monkeypatch):
    from src.auth.deps import is_lan_request
    from starlette.requests import Request

    def req(host, headers=()):
        return Request({"type": "http", "client": (host, 1234), "headers": [(k.encode(), v.encode()) for k, v in headers]})
    assert is_lan_request(req("192.168.35.10")) and is_lan_request(req("127.0.0.1"))
    assert not is_lan_request(req("8.8.8.8"))
    assert not is_lan_request(req("127.0.0.1", [("cf-connecting-ip", "1.2.3.4")]))   # 터널을 거친 요청
    assert not is_lan_request(req("testclient"))

    monkeypatch.setenv("QUICK_LOGIN_LAN_ONLY", "1")
    monkeypatch.setenv("DEMO_OWNER_USERNAME", "demo-owner")
    monkeypatch.setenv("DEMO_OWNER_PASSWORD", "owner-pass-1")
    auth_store.create_user("demo-owner", "owner-pass-1", "owner", owned_ev_id="EV-Q-1")
    assert client.post("/auth/quick-login", json={"role": "owner"}).status_code == 403   # TestClient 접속지는 사설 IP가 아님
    assert client.get("/auth/quick-login/available").json()["owner"] is False
    monkeypatch.setenv("QUICK_LOGIN_LAN_ONLY", "0")
    assert client.post("/auth/quick-login", json={"role": "owner"}).status_code == 200


# ------------------------------------------------------------------ C04 · Y2A02 · E01
def test_effect_conditions_have_vehicle_type_axis_and_coverage(small_cfg):
    import numpy as np
    from src.datagen.ev import generate_ev_telemetry
    from src.datagen.grid import generate_grid_signal
    from src.optimizer.effect_metrics import compute_effect_metrics
    from src.optimizer.poc_compare import aggregate_comparison, run_v2g_optimization
    rng = np.random.default_rng(1)
    ev, grid = generate_ev_telemetry(small_cfg, rng), generate_grid_signal(small_cfg, rng)
    run = run_v2g_optimization(ev, grid, small_cfg)
    e = compute_effect_metrics(run, aggregate_comparison(ev, run["schedule"]), ev, grid, 0.9)
    assert e["conditions"]["vehicle_type"] and set(e["conditions"]["coverage"]) == {"season", "time_band", "start_soc", "vehicle_type", "day_type"}
    from src.reporting import limitation_rows
    rows = limitation_rows({"kpi": {"data_provenance": {"ev_telemetry": "synthetic"}}, "effect_verification": e})
    assert rows and all(len(r) == 4 for r in rows) and rows[0][2] == "한계"


# ------------------------------------------------------------------ A15
def test_rows_carry_source_and_generation():
    from src.pipeline.source import annotate_rows
    ev = pd.DataFrame({"ev_id": ["A", "B"]})
    real = annotate_rows(ev, "ev_telemetry", {"ev_telemetry": "measured"})
    assert set(real["data_source"]) == {"obd"} and set(real["generation"]) == {"measured"}
    demo = annotate_rows(ev, "ev_telemetry", {"ev_telemetry": "synthetic"})
    assert set(demo["data_source"]) == {"synthetic"} and set(demo["generation"]) == {"synthetic"}
    ch = pd.DataFrame({"session_id": ["s1", "s2"], "source": ["rest", "synthetic"]})
    mixed = annotate_rows(ch, "charger_sessions", {"charger_sessions": "measured"})
    assert list(mixed["data_source"]) == ["cpo", "synthetic"]                 # 부분 실증 보완분은 행 단위로 합성 표시
    kept = annotate_rows(mixed.assign(generation=["inferred", "synthetic"]), "charger_sessions", {"charger_sessions": "measured"})
    assert list(kept["generation"]) == ["inferred", "synthetic"]               # 이미 있는 값은 덮어쓰지 않는다


# ------------------------------------------------------------------ Y2E05 VoC 설문
def test_survey_create_respond_once_and_aggregate():
    _admin()
    body = {"title": "1차 만족도", "questions": [
        {"type": "scale", "text": "만족하시나요?"},
        {"type": "choice", "text": "도움 된 기능", "options": ["추천", "리포트"]},
        {"type": "text", "text": "개선점"}]}
    assert client.post("/admin/surveys", json={"title": "x", "questions": [{"type": "choice", "text": "a", "options": ["only"]}]}).status_code == 422
    sid = client.post("/admin/surveys", json=body).json()["id"]
    client.post("/auth/logout")
    auth_store.create_user("owner-voc", "owner-pass-1", "owner", owned_ev_id="EV-VOC-1")
    assert client.post("/auth/login", json={"username": "owner-voc", "password": "owner-pass-1"}).status_code == 200
    mine = client.get("/owner/surveys").json()["surveys"]
    assert mine[0]["id"] == sid and mine[0]["answered"] is False
    assert client.post(f"/owner/surveys/{sid}/responses", json={"answers": {"q1": 9, "q2": "추천"}}).status_code == 422
    assert client.post(f"/owner/surveys/{sid}/responses", json={"answers": {"q1": 4, "q2": "추천", "q3": "좋아요"}}).status_code == 200
    assert client.post(f"/owner/surveys/{sid}/responses", json={"answers": {"q1": 5, "q2": "리포트"}}).status_code == 409
    assert client.get("/owner/surveys").json()["surveys"][0]["answered"] is True
    client.post("/auth/logout")
    _admin_login = client.post("/auth/login", json={"username": "admin-gap", "password": "admin-pass-1"})
    assert _admin_login.status_code == 200
    res = client.get(f"/admin/surveys/{sid}/results").json()
    q1, q2, q3 = res["questions"]
    assert res["responses"] == 1 and q1["average"] == 4.0 and q2["counts"] == {"추천": 1, "리포트": 0} and q3["texts"] == ["좋아요"]
    csv = client.get(f"/admin/surveys/{sid}/results.csv").text
    assert "EV-VOC-1" not in csv and "좋아요" in csv                         # 차량 ID 없이 내보낸다
    assert client.post(f"/admin/surveys/{sid}/close").json()["status"] == "closed"


# ------------------------------------------------------------------ 공용 로딩 표시
def test_every_screen_loads_shared_loading_overlay():
    js = client.get("/loading.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"] and "axQuiet" in js.text
    for path in ("/dashboard", "/admin", "/approvals", "/dev", "/app", "/login"):
        assert '<script src="/loading.js"></script>' in client.get(path).text, path
    assert "axLoading.quiet(loadDashboard)" in client.get("/dashboard").text      # 자동 새로고침은 화면을 가리지 않는다


# ------------------------------------------------------------------ Y2B02 운영 데이터 재학습 사이클
def test_retrain_cycle_on_current_data_bootstraps_then_checks_drift(tmp_path, medium_cfg):
    import numpy as np
    from src.automation.model_registry import ModelRegistry
    from src.automation.scheduler import run_cycle_on_data
    from src.datagen.ev import generate_ev_telemetry
    from src.datagen.grid import generate_grid_signal
    from src.pipeline.store import build_city_hourly
    rng = np.random.default_rng(3)
    city = build_city_hourly(generate_ev_telemetry(medium_cfg, rng), generate_grid_signal(medium_cfg, rng), medium_cfg)
    reg = ModelRegistry("ev_charging_demand_kw", base_dir=tmp_path)
    first = run_cycle_on_data(reg, city, "ev_charging_demand_kw", "synthetic")
    assert first["action"] == "BOOTSTRAPPED" and reg.active_version() == 1
    second = run_cycle_on_data(reg, city, "ev_charging_demand_kw", "synthetic")
    assert second["action"] in ("NO_RETRAIN", "PROMOTED", "REJECTED_KEEP_ACTIVE") and "drift_pct" in second
    assert reg.load()[2]["data_source"] == "synthetic"


# ------------------------------------------------------------------ Y2E01 타 지역 비교
def test_region_compare_marks_uncollected_and_compares_collected(tmp_path, monkeypatch):
    from src.pipeline import region_compare as rc
    monkeypatch.setattr(rc, "REGION_DIR", tmp_path / "regions")
    ts = pd.date_range("2026-12-01", periods=48, freq="h")
    grid = pd.DataFrame({"timestamp": ts, "temperature_c": [5.0] * 48})
    out = rc.compare(grid, "합성(모의)")
    assert out["regions"][0]["role"] == "실증" and out["regions"][0]["mean_c"] == 5.0
    assert all(r["status"] == "수집 전" for r in out["regions"][1:])            # 값을 지어내지 않는다
    fake = lambda start, end, station: pd.DataFrame({"timestamp": ts, "temperature_c": [-2.0] * 24 + [1.0] * 24})
    res = rc.sync_region_temperature("daejeon", ts[0], ts[-1], fetch=fake)
    assert res["station"] == "133" and res["stored_hours"] == 48
    dj = next(r for r in rc.compare(grid, "합성(모의)")["regions"] if r["id"] == "daejeon")
    assert dj["status"] == "수집됨" and dj["below_zero_pct"] == 50.0 and dj["mean_diff_c"] == round(-0.5 - 5.0, 1)
    _admin()
    assert client.post("/ingest/regions/nowhere/sync-temperature?start=2026-12-01&end=2026-12-02").status_code == 404
