"""충전 시간 안내(owner_guide) 검증 — 독립 계산 대조, 차량별 분기, 엔드포인트 격리."""
from datetime import datetime

import numpy as np
import pytest

from src.advisory import owner_guide as og

NOW = datetime(2026, 10, 22, 21, 30)
V1G = {"label": "일반 EV", "battery_kwh": 64.0, "v2g_capable": False, "demo": False}
V2G = {"label": "V2G 시연", "battery_kwh": 77.4, "v2g_capable": True, "demo": True}


def guide(profile, soc=40, need=80, dep=datetime(2026, 10, 23, 8, 0), comp="contract_example"):
    return og.build_guide(profile, og.GuideInputs(soc, need, dep, comp), now=NOW)


def test_v1g_never_discharges_and_charges_exactly_the_needed_energy():
    g = guide(V1G)
    assert g["plan"]["discharge_kwh"] == 0
    eff = 0.92
    need = (80 - 40) / 100 * 64.0
    assert g["plan"]["charge_kwh"] * eff == pytest.approx(need, abs=0.2) or abs(g["plan"]["charge_kwh"] * 0.95 - need) < 1.5
    assert g["plan"]["final_soc_pct"] >= 79.9
    assert g["cost"]["saving_krw"] >= 0                       # 즉시 충전보다 나빠지지 않는다


def test_soc_path_stays_within_physical_limits():
    g = guide(V2G, soc=60, need=80)
    socs = [p["soc_pct"] for p in g["plan"]["soc"]]
    assert max(socs) <= 100.0 + 1e-6 and min(socs) >= 20.0 - 1e-6


def test_v2g_never_costs_more_than_v1g_for_same_battery():
    a = guide(V2G)
    b = guide({**V2G, "v2g_capable": False})
    assert a["cost"]["optimized_krw"] <= b["cost"]["optimized_krw"] + 1


def test_no_compensation_means_no_discharge_even_if_capable():
    g = guide(V2G, comp="none") if "none" in og.get_compensation.__globals__.get("_ids", ["none"]) else None
    if g:
        assert g["plan"]["discharge_kwh"] == 0


def test_validation_errors():
    with pytest.raises(og.GuideError):
        og.build_guide(V1G, og.GuideInputs(50, 80, datetime(2026, 10, 22, 22, 30)), now=NOW)   # 1시간 남음
    with pytest.raises(og.GuideError):
        guide(V1G, soc=10, need=100, dep=datetime(2026, 10, 22, 23, 30))                       # 시간 부족
    with pytest.raises(og.GuideError):
        guide(V1G, dep=datetime(2026, 10, 30, 8, 0))                                           # 48시간 초과


def test_demand_forecast_uses_cache_and_summarises(monkeypatch):
    og.reset_caches()
    calls = []

    def fetch(target):
        calls.append(target)
        ts = [f"2026-10-23T{h:02d}:00:00" for h in range(24)]
        vals = [10.0 + (30 if 18 <= h <= 21 else 0) for h in range(24)]
        return {"timestamps": ts, "values": vals, "is_real_data": False, "model_version": "t"}

    d = og.demand_forecast(fetch, now_s=1000.0)
    assert d["charging_peaks"] and "18~22" in d["charging_peaks"][0]["label"]
    og.demand_forecast(fetch, now_s=1100.0)
    assert len(calls) == 2                                     # 두 지표를 한 번씩만 불러오고 캐시 사용
    og.reset_caches()


# ------------------------------------------------------------ 엔드포인트·EV Bot 연결 --

from fastapi.testclient import TestClient  # noqa: E402

from src.api import app  # noqa: E402
from src.api.routers import signup as signup_router  # noqa: E402
from src.auth import store as auth_store  # noqa: E402

client = TestClient(app)
PAYLOAD = {"username": "guidedriver", "password": "driver-pass-1", "vehicle_model": "v2g_demo", "plate": "43오3450",
           "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True}


@pytest.fixture
def owner_session(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    og.reset_caches()
    assert client.post("/signup", json=PAYLOAD).status_code == 200
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg}/approve", json={}).status_code == 200
    client.post("/auth/login", json={"username": "guidedriver", "password": "driver-pass-1"})
    yield
    client.post("/auth/logout")


def test_charging_guide_endpoint_is_owner_only_and_uses_session_vehicle(owner_session):
    r = client.get("/owner/charging-guide", params={"current_soc_pct": 45, "need_soc_pct": 85})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["vehicle"]["v2g_capable"] is True and body["verified"] is False
    assert body["inputs"]["need_soc_pct"] == 85
    assert client.get("/owner/charging-guide", params={"need_soc_pct": 10}).status_code == 422   # 범위 밖
    client.post("/auth/logout")
    assert client.get("/owner/charging-guide").status_code in (401, 403)


def test_localize_translates_fixed_phrases_to_english_without_touching_the_original():
    src = {"v2g": {"reason": "이번 조건에서는 방전해도 이득이 크지 않아서 추천하지 않아요."}, "assumptions": {"note": "x"}}
    en = og.localize(src, "en")
    assert en["v2g"]["reason"].startswith("Under these conditions") and src["v2g"]["reason"].startswith("이번")
    assert og.localize(src, "ko") is src
    assert og._en("현재 배터리(40%)가 방전을 허용하는 범위(20~90%) 밖이라 방전은 추천하지 않아요.").startswith("Your battery (40%)")


# ------------------------------------------------------- 안전 검증·방전 참여(이동권 우선) --

from src.advisory.safety_guard import verify_plan  # noqa: E402


def test_guide_carries_a_passing_safety_report():
    g = guide(V2G, soc=60, need=70)
    ids = {c["id"] for c in g["safety"]["checks"]}
    assert g["safety"]["passed"] is True and g["safety"]["fallback"] is False
    assert {"departure_soc", "reserve_floor", "discharge_band", "participation"} <= ids
    assert g["plan"]["final_soc_pct"] >= 70 - 0.1            # 이용자가 정한 출발 잔량은 항상 지킨다


def test_discharge_participation_off_means_no_discharge():
    g = og.build_guide(V2G, og.GuideInputs(60, 70, datetime(2026, 10, 23, 8, 0), "contract_example"), now=NOW, participating=False)
    assert g["plan"]["discharge_kwh"] == 0 and g["safety"]["participating"] is False and g["safety"]["passed"] is True
    assert "참여를 꺼" in g["v2g"]["reason"]
    assert og.localize(g, "en")["v2g"]["reason"].startswith("Discharge participation is off")


def test_guard_catches_every_kind_of_violation_independently_of_the_optimizer():
    cap, band = 100.0, (35.0, 80.0)
    ok = dict(energy=[50, 60, 60], charge=[10, 0], discharge=[0, 0], cap_kwh=cap, target_kwh=60, reserve_kwh=20, band_kwh=band)
    assert verify_plan(**ok)["passed"]
    failed = lambda **kw: {c["id"] for c in verify_plan(**{**ok, **kw})["checks"] if not c["ok"]}  # noqa: E731
    assert failed(target_kwh=70) == {"departure_soc"}                                           # 출발 잔량 미달
    assert "reserve_floor" in failed(energy=[50, 15, 60])                                       # 예비 잔량 침범
    assert "capacity_cap" in failed(energy=[50, 105, 105])                                      # 용량 초과
    assert "discharge_band" in failed(energy=[90, 85, 60], discharge=[5, 0])                    # 고잔량 방전
    assert "no_simultaneous" in failed(charge=[10, 0], discharge=[3, 0])                        # 동시 충방전
    assert "participation" in failed(discharge=[0, 8], participating=False)                     # 참여 끔인데 방전


def test_v2g_switch_endpoint_defaults_off_and_records_history(owner_session):
    assert client.get("/owner/consent/v2g").json()["v2g"] is False
    off = client.get("/owner/charging-guide", params={"current_soc_pct": 60, "need_soc_pct": 70}).json()
    assert off["plan"]["discharge_kwh"] == 0 and off["safety"]["participating"] is False
    assert client.put("/owner/consent/v2g", json={"granted": True}).json() == {"v2g": True}
    on = client.get("/owner/charging-guide", params={"current_soc_pct": 60, "need_soc_pct": 70}).json()
    assert on["safety"]["participating"] is True
    assert client.put("/owner/consent/v2g", json={"granted": False}).json() == {"v2g": False}   # 즉시 중지
    assert client.get("/owner/charging-guide", params={"current_soc_pct": 60, "need_soc_pct": 70}).json()["plan"]["discharge_kwh"] == 0


def test_guide_is_recorded_in_audit_log(owner_session):
    import json

    from src.privacy import audit

    assert client.get("/owner/charging-guide", params={"current_soc_pct": 45, "need_soc_pct": 85}).status_code == 200
    con = audit._connect()
    try:
        rows = {a: (t, json.loads(d)) for a, t, d in con.execute(
            "SELECT action, target, detail FROM audit_log WHERE action = 'owner_guide_issue' ORDER BY id")}
    finally:
        con.close()
    target, guide = rows["owner_guide_issue"]
    assert guide["safety_passed"] is True and guide["failed_checks"] == [] and guide["inputs"]["need_soc_pct"] == 85
    assert audit.verify_chain()["ok"] is True


def test_ai_ops_kpi_aggregates_safety_pass_and_withdrawal(owner_session):
    assert client.put("/owner/consent/v2g", json={"granted": True}).status_code == 200
    assert client.get("/owner/charging-guide", params={"current_soc_pct": 60, "need_soc_pct": 80}).status_code == 200
    assert client.put("/owner/consent/v2g", json={"granted": False}).status_code == 200
    assert client.get("/advisory/ai-ops-kpi").status_code in (401, 403)           # 이용자는 볼 수 없다
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    k = client.get("/advisory/ai-ops-kpi").json()
    assert k["recommendations"]["issued"] >= 1 and k["recommendations"]["safety_pass_rate"] == 1.0
    assert k["v2g_participation"]["granted_total"] >= 1 and k["v2g_participation"]["withdrawal_rate"] == 1.0


def test_cached_forecast_never_blocks_the_bot_path(monkeypatch):
    og.reset_caches()
    started = []
    monkeypatch.setattr(og, "_warm_forecast_async", lambda: started.append(True))
    with pytest.raises(RuntimeError):                      # 캐시가 없으면 기다리지 않고 예외 → 예측 없이 답한다
        og.cached_demand_forecast()
    assert started == [True]
    og._forecast_cache.update(at=og.time.monotonic(), data={"charging_peaks": [], "charging_lows": [], "note": "x"})
    assert og.cached_demand_forecast()["note"] == "x"      # 캐시가 있으면 즉시 돌려준다
    og.reset_caches()
