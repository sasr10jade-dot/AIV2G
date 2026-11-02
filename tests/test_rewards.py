"""미션·시범 포인트 — 서버 판정, 동의 기반 적립, 중복 방지, 월 상한, 권한."""
import pytest
from fastapi.testclient import TestClient

from src.advisory import owner_guide as og
from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.copilot import owner_bot
from src.experiment import incentive
from src.rewards import service as rewards

client = TestClient(app)
PAYLOAD = {"username": "rewarddriver", "password": "driver-pass-1", "vehicle_model": "v2g_demo", "plate": "43오3450",
           "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True}


@pytest.fixture
def owner_session(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    # 미션 판정은 '안내가 발급됐다'는 감사로그만 본다 — 최적화 계산(수 초)은 가벼운 결과로 대체한다
    monkeypatch.setattr(og, "build_guide", lambda profile, inputs, **kw: {
        "inputs": {"current_soc_pct": inputs.current_soc_pct, "need_soc_pct": inputs.need_soc_pct},
        "safety": {"passed": True, "participating": False, "fallback": False, "checks": []},
        "v2g": {"recommended": False}, "verified": False})
    auth_store.create_user("opuser", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    owner_bot.reset_rate_limit()
    og.reset_caches()
    assert client.post("/signup", json=PAYLOAD).status_code == 200
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg}/approve", json={}).status_code == 200
    client.post("/auth/login", json={"username": "rewarddriver", "password": "driver-pass-1"})
    yield
    client.post("/auth/logout")


def _by_id(body):
    return {m["id"]: m for m in body["missions"]}


def test_no_points_without_incentive_consent_even_when_achieved(owner_session):
    assert client.put("/owner/consent/notify", json={"granted": True}).status_code == 200
    body = client.get("/owner/missions").json()
    m = _by_id(body)["notify_on"]
    assert m["achieved"] is True and m["awarded_points"] is None
    assert body["points"]["balance"] == 0 and body["incentive_consent"] is False and body["verified"] is False


def test_points_are_earned_once_after_consent_and_stop_after_withdrawal(owner_session):
    client.put("/owner/consent/notify", json={"granted": True})
    client.put("/owner/consent/incentive", json={"granted": True})
    first = client.get("/owner/missions").json()
    assert _by_id(first)["notify_on"]["awarded_points"] == 50 and first["points"]["balance"] == 50
    again = client.get("/owner/missions").json()
    assert again["points"]["balance"] == 50                                    # 같은 미션·기간은 한 번만
    # 안내를 확인하면(서버 로그 기준) 시작 미션이 달성된다
    assert client.get("/owner/charging-guide", params={"current_soc_pct": 50, "need_soc_pct": 80}).status_code == 200
    third = client.get("/owner/missions").json()
    assert _by_id(third)["first_guide"]["awarded_points"] == 50 and third["points"]["balance"] == 100
    # 철회하면 이후 적립이 멈추고, 쌓은 포인트는 남는다
    client.put("/owner/consent/incentive", json={"granted": False})
    for _ in range(2):
        client.get("/owner/charging-guide", params={"current_soc_pct": 50, "need_soc_pct": 80})
    after = client.get("/owner/missions").json()
    assert after["points"]["balance"] == 100 and _by_id(after)["guide_weekly"]["awarded_points"] is None
    ledger = client.get("/owner/points").json()["ledger"]
    assert {r["mission_id"] for r in ledger} == {"notify_on", "first_guide"}


def test_monthly_cap_limits_earnings(owner_session, monkeypatch):
    policy = incentive.IncentivePolicy.load()
    capped = incentive.IncentivePolicy(**{**policy.__dict__, "monthly_cap_points": 70})
    monkeypatch.setattr(incentive.IncentivePolicy, "load", classmethod(lambda cls, path=None: capped))
    client.put("/owner/consent/notify", json={"granted": True})
    client.put("/owner/consent/incentive", json={"granted": True})
    client.get("/owner/charging-guide", params={"current_soc_pct": 50, "need_soc_pct": 80})
    body = client.get("/owner/missions").json()
    assert body["points"]["balance"] == 70 and body["points"]["month_cap"] == 70    # 50 + (50 중 20만)


def test_mission_endpoints_are_owner_only_and_english_titles(owner_session):
    en = client.get("/owner/missions", params={"lang": "en"}).json()
    assert _by_id(en)["notify_on"]["title"] == "Turn on notifications"
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    for path in ("/owner/missions", "/owner/points", "/owner/consent/incentive"):
        assert client.get(path).status_code in (401, 403)
    client.post("/auth/logout")
    assert client.get("/owner/missions").status_code in (401, 403)
    assert rewards.load_config()["verified"] is False


# ------------------------------------------------ 추천대로 충전했는지 확인(데이터 대조) --

import pandas as pd  # noqa: E402

VER = {"tolerance_hours": 1, "min_follow_share": 0.7, "min_charged_ratio": 0.5}


def _hourly(charges: dict, start="2026-10-20T00:00:00", hours=48):
    ts = pd.date_range(start, periods=hours, freq="h")
    kw = [float(charges.get(t.isoformat(), 0.0)) for t in ts]
    return pd.DataFrame({"timestamp": ts, "ev_id": "EV-001", "charging_kw": kw})


def _plan(start="2026-10-20T23:00:00", end="2026-10-21T02:00:00", kwh=12.0, departure="2026-10-21T08:00:00"):
    return {"issued_kst": "2026-10-20T20:10:00", "departure": departure, "intervals": [{"start": start, "end": end, "kwh": kwh}]}


def test_check_follow_ok_when_charged_inside_recommended_window():
    h = _hourly({"2026-10-20T23:00:00": 4, "2026-10-21T00:00:00": 4, "2026-10-21T01:00:00": 4})
    assert rewards.check_follow(_plan(), h, VER) == "ok"
    near = _hourly({"2026-10-20T22:00:00": 6, "2026-10-21T00:00:00": 6})      # ±1시간 허용 안
    assert rewards.check_follow(_plan(), near, VER) == "ok"


def test_check_follow_no_when_charged_elsewhere_or_too_little():
    elsewhere = _hourly({"2026-10-21T05:00:00": 6, "2026-10-21T06:00:00": 6})
    assert rewards.check_follow(_plan(), elsewhere, VER) == "no"
    little = _hourly({"2026-10-21T00:00:00": 2})                                # 추천량의 50% 미만
    assert rewards.check_follow(_plan(), little, VER) == "no"
    assert rewards.check_follow(_plan(), _hourly({}), VER) == "no"              # 충전 기록이 있지만 충전하지 않음


def test_check_follow_pending_until_records_reach_departure_and_unavailable_without_records():
    short = _hourly({"2026-10-20T23:00:00": 6}, hours=20)                        # 기록이 출발 시각 전에서 끝남
    assert rewards.check_follow(_plan(), short, VER) == "pending"
    assert rewards.check_follow(_plan(), None, VER) == "unavailable"
    assert rewards.check_follow(_plan(), _hourly({}).iloc[0:0], VER) == "unavailable"


def test_verified_follow_mission_counts_days_and_awards_with_consent(owner_session, monkeypatch):
    ev = client.get("/owner/me").json()["ev_id"]
    hourly = _hourly({"2026-10-20T23:00:00": 6, "2026-10-21T00:00:00": 6, "2026-10-22T23:00:00": 6, "2026-10-23T00:00:00": 6},
                     start="2026-10-19T00:00:00", hours=24 * 7)
    hourly["ev_id"] = ev
    monkeypatch.setattr(rewards, "_hourly_charging", lambda e: hourly)
    for issued, dep, s, e in (("2026-10-20T20:00:00", "2026-10-21T08:00:00", "2026-10-20T23:00:00", "2026-10-21T02:00:00"),
                              ("2026-10-20T21:00:00", "2026-10-21T08:00:00", "2026-10-20T23:00:00", "2026-10-21T02:00:00"),   # 같은 날 중복
                              ("2026-10-22T20:00:00", "2026-10-23T08:00:00", "2026-10-22T23:00:00", "2026-10-23T02:00:00")):
        rewards.record_plan(ev, {"plan": {"charge": [{"start": s, "end": e, "kwh": 12.0}]}, "period": {"departure": dep}},
                            issued_kst=pd.Timestamp(issued))
    client.put("/owner/consent/incentive", json={"granted": True})
    now = pd.Timestamp("2026-10-23T12:00:00")                                    # 같은 주(10/19~10/25)
    body = rewards.evaluate(ev, "rewarddriver", "ko", now_kst=now)
    m = _by_id(body)["follow_charge_weekly"]
    assert m["progress"] == 2 and m["achieved"] is True and m["awarded_points"] == 200 and m["verification"] == "ok"


def test_follow_mission_is_unavailable_for_vehicles_without_obd(owner_session):
    m = _by_id(client.get("/owner/missions").json())["follow_charge_weekly"]
    assert m["verification"] == "unavailable" and m["achieved"] is False


# ------------------------------------------------ 추천 응답(추천대로 할게요 / 나중에) --

def _stub_guide_with_plan(monkeypatch):
    monkeypatch.setattr(og, "build_guide", lambda profile, inputs, **kw: {
        "inputs": {"current_soc_pct": inputs.current_soc_pct, "need_soc_pct": inputs.need_soc_pct},
        "plan": {"charge": [{"start": "2026-10-24T01:00:00", "end": "2026-10-24T04:00:00", "kwh": 10.0}], "discharge": []},
        "period": {"departure": "2026-10-24T08:00:00"},
        "safety": {"passed": True, "participating": False, "fallback": False, "checks": []},
        "v2g": {"recommended": False}, "verified": False})


def test_guide_response_is_saved_for_own_plan_and_feeds_acceptance_rate(owner_session, monkeypatch):
    _stub_guide_with_plan(monkeypatch)
    g1 = client.get("/owner/charging-guide", params={"current_soc_pct": 40, "need_soc_pct": 80}).json()
    g2 = client.get("/owner/charging-guide", params={"current_soc_pct": 41, "need_soc_pct": 80}).json()
    assert g1["guide_id"] and g2["guide_id"] and g1["guide_id"] != g2["guide_id"]
    assert client.post(f"/owner/guide/{g1['guide_id']}/respond", json={"status": "accepted"}).status_code == 200
    assert client.post(f"/owner/guide/{g2['guide_id']}/respond", json={"status": "declined"}).status_code == 200
    assert client.post(f"/owner/guide/{g2['guide_id']}/respond", json={"status": "accepted"}).status_code == 200   # 마음이 바뀌면 바꿀 수 있다
    assert client.post(f"/owner/guide/{g1['guide_id']}/respond", json={"status": "maybe"}).status_code == 422
    assert client.post("/owner/guide/99999/respond", json={"status": "accepted"}).status_code == 404
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    rec = client.get("/advisory/ai-ops-kpi").json()["recommendations"]
    assert rec["accepted"] == 2 and rec["declined"] == 0 and rec["acceptance_rate"] == 1.0


def test_cannot_respond_to_another_members_recommendation(owner_session, monkeypatch):
    _stub_guide_with_plan(monkeypatch)
    gid = client.get("/owner/charging-guide", params={"current_soc_pct": 40, "need_soc_pct": 80}).json()["guide_id"]
    con = rewards._connect()
    try:
        con.execute("UPDATE guide_plans SET ev_id='EV-SOMEONE-ELSE' WHERE id=?", (gid,))
        con.commit()
    finally:
        con.close()
    assert client.post(f"/owner/guide/{gid}/respond", json={"status": "accepted"}).status_code == 404   # 남의 추천에는 응답할 수 없다


def test_old_guide_plans_table_gets_response_columns(tmp_path):
    import sqlite3

    con = sqlite3.connect(tmp_path / "old.db")
    con.execute("CREATE TABLE guide_plans (id INTEGER PRIMARY KEY AUTOINCREMENT, ev_id TEXT NOT NULL, issued_kst TEXT NOT NULL, departure TEXT, intervals TEXT NOT NULL)")
    rewards._plans_table(con)
    assert {"response", "responded_at"} <= {r[1] for r in con.execute("PRAGMA table_info(guide_plans)")}
