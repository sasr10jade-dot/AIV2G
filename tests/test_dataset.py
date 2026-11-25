"""실증 데이터셋(결합 레코드)·거점 세션 학습 반영·충전 인증 태그 결합 — 2026-11-23 결정(평가의견 4·14 보완) 테스트."""
import sqlite3

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src import kpi
from src.api import app
from src.auth import store as auth_store
from src.datagen.config import SimConfig
from src.pipeline import dataset, store

client = TestClient(app)


@pytest.fixture(autouse=True)
def users(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    yield
    client.post("/auth/logout")


def _admin():
    auth_store.create_user("admin-ds", "admin-pass-1", "admin")
    assert client.post("/auth/login", json={"username": "admin-ds", "password": "admin-pass-1"}).status_code == 200


def _grid(start, hours, drop_hour=None):
    ts = pd.date_range(start, periods=hours, freq="h")
    g = pd.DataFrame({"timestamp": ts, "power_demand_mw": 100.0, "renewable_gen_mw": 10.0, "temperature_c": 5.0,
                      "price_krw_per_kwh": 120.0, "is_holiday": 0})
    if drop_hour is not None:
        g.loc[g["timestamp"] == pd.Timestamp(drop_hour), "temperature_c"] = None   # 공공데이터 결측 시간
    return g


def _ev():
    ts = pd.to_datetime(["2026-12-01 07:00", "2026-12-01 08:00", "2026-12-01 09:00", "2026-12-01 22:00", "2026-12-01 23:00"])
    return pd.DataFrame({"timestamp": ts, "ev_id": "EV-A", "driving": [True, True, False, False, False],
                         "connected": [False, False, False, True, True], "charging_kw": [0, 0, 0, 7.0, 7.0],
                         "soc_pct": [80.0, 72.0, 70.0, 70.0, 80.0], "vehicle_model": "m1", "battery_capacity_kwh": 70.0})


def _ch():
    return pd.DataFrame({"session_id": ["S1", "S2", "S3"], "charger_id": ["C1", "C2", "C3"],
                         "start_ts": pd.to_datetime(["2026-12-01 22:10", "2026-12-01 10:00", "2026-12-01 13:30"]),
                         "duration_h": [2.0, 1.5, 0.5], "energy_kwh": [14.0, 9.0, 20.0],
                         "ev_id": ["EV-A", None, None], "site_id": ["SITE-01", "SITE-01", "SITE-02"]})


def test_one_record_is_event_with_vehicle_charger_and_ai_blocks():
    """1건 = 운행·차량 충전·거점 충전 이벤트 × (① 차량·충전기 결합 + ② AI 학습 블록). 서포터즈 세션은 A형 충전에 결합해 한 번만 센다."""
    ds = dataset.build_dataset(_ev(), _ch(), _grid("2026-12-01", 24))
    assert sorted(ds["kind"]) == ["charge", "drive", "site_charge", "site_charge"]
    charge = ds[ds["kind"] == "charge"].iloc[0]
    assert charge["charger_id"] == "C1" and charge["charger_session_id"] == "S1"          # 차량 충전 ↔ 충전기 세션 결합
    assert charge["soc_start_pct"] == 70.0 and charge["energy_kwh"] == 14.0
    drive = ds[ds["kind"] == "drive"].iloc[0]
    assert drive["soc_start_pct"] == 80.0 and drive["soc_end_pct"] == 70.0 and drive["energy_kwh"] == 0.0   # 종료 SOC = 운행 직후(09시) 관측
    assert charge["soc_end_pct"] == 80.0                                                                    # 다음 관측 없음 → 마지막 관측
    assert ds["context_ok"].all() and ds["counted"].all() and (ds["temperature_c"] == 5.0).all()
    s = dataset.summarize(ds)
    assert s["counted"] == 4 and s["join_rate_pct"] == 100.0 and s["supporter_charges_linked_to_charger"] == 1
    assert s["by_kind"]["drive"]["counted"] == 1 and s["by_year"]["Y1"]["counted"] == 4


def test_event_without_ai_context_is_not_counted():
    ds = dataset.build_dataset(_ev(), _ch(), _grid("2026-12-01", 24, drop_hour="2026-12-01 13:00"))
    s = dataset.summarize(ds)
    assert s["events"] == 4 and s["counted"] == 3 and s["join_rate_pct"] == 75.0 and s["join_rate_ok"] is False
    y = kpi.count_by_year(_ev(), _ch(), grid_df=_grid("2026-12-01", 24, drop_hour="2026-12-01 13:00"))[0][0]
    assert (y.charger_sessions, y.vehicle_days, y.records) == (1, 2, 3)


def test_site_sessions_are_spread_over_hours_and_join_training_label():
    """거점 세션(서포터즈 제외)을 시간 단위 kW로 나눠 학습 라벨(충전수요)에 더한다."""
    site = store.site_charging_hourly(_ch(), {"EV-A"})
    by = dict(zip(site["timestamp"], site["site_charging_demand_kw"]))
    assert by[pd.Timestamp("2026-12-01 10:00")] == pytest.approx(6.0) and by[pd.Timestamp("2026-12-01 11:00")] == pytest.approx(3.0)
    assert by[pd.Timestamp("2026-12-01 13:00")] == pytest.approx(20.0)
    assert pd.Timestamp("2026-12-01 22:00") not in by                       # 서포터즈 세션은 OBD로 이미 들어감
    city = store.build_city_hourly(_ev(), _grid("2026-12-01", 24), SimConfig(), charger_df=_ch())
    row = city.set_index("timestamp")
    assert row.loc["2026-12-01 10:00", "ev_charging_demand_kw"] == pytest.approx(6.0)
    assert row.loc["2026-12-01 22:00", "ev_charging_demand_kw"] == pytest.approx(7.0)
    assert row["ev_charging_demand_kw"].sum() == pytest.approx(row["supporter_charging_demand_kw"].sum() + 29.0)
    old = store.build_city_hourly(_ev(), _grid("2026-12-01", 24), SimConfig())
    assert "site_charging_demand_kw" not in old.columns                      # charger_df 없으면 기존 동작


def test_charge_tag_links_pushed_session_to_supporter_and_raw_tag_not_stored(tmp_path):
    from src.registry import charge_tags
    reg = charge_tags.register("EV-A", "rfid-0001")
    assert reg["tag_pseudo_id"].startswith("PT-") and "rfid" not in (tmp_path / "charge_tags.json").read_text(encoding="utf-8").lower()
    payload = {"batch_date": "2026-12-01", "sessions": [
        {"session_id": "T1", "charger_id": "C1", "start_ts": "2026-12-01T22:10:00", "duration_h": 2.0, "energy_kwh": 14.0,
         "id_tag": "RFID-0001", "soc_start_pct": 70, "soc_end_pct": 90, "vehicle_model": "m1"},
        {"session_id": "T2", "charger_id": "C2", "start_ts": "2026-12-01T10:00:00", "duration_h": 1.5, "energy_kwh": 9.0}]}
    r = client.post("/cpo/ingest/niceinfra", json=payload)
    assert r.status_code == 200 and r.json()["stored"] == 2 and r.json()["linked_to_supporter"] == 1
    con = sqlite3.connect(store.CURATED_DB)
    rows = dict(con.execute("SELECT session_id, ev_id FROM charger_sessions").fetchall())
    cols = {c[1] for c in con.execute("PRAGMA table_info(charger_sessions)")}
    soc = con.execute("SELECT soc_start_pct FROM charger_sessions WHERE session_id='niceinfra:T1'").fetchone()[0]
    con.close()
    assert rows == {"niceinfra:T1": "EV-A", "niceinfra:T2": None} and "id_tag" not in cols and soc == 70.0


def test_charge_tag_admin_api():
    _admin()
    r = client.post("/cpo/charge-tags", json={"ev_id": "EV-B", "id_tag": "card-77"})
    assert r.status_code == 200
    tid = r.json()["tag_pseudo_id"]
    lst = client.get("/cpo/charge-tags").json()
    assert lst["count"] == 1 and lst["items"][0]["ev_id"] == "EV-B" and "card-77" not in str(lst)
    assert client.delete(f"/cpo/charge-tags/{tid}").status_code == 200
    assert client.get("/cpo/charge-tags").json()["count"] == 0


def test_dataset_api_and_forecast_targets():
    from src.model import forecast_service
    assert forecast_service.ALLOWED_TARGETS == {"ev_charging_demand_kw", "ev_available_v2g_kwh"}   # 전력수요는 입력 변수
    _admin()
    r = client.get("/kpi/dataset?sample=3")
    assert r.status_code == 200, r.text
    d = r.json()
    assert {"drive", "charge", "site_charge"} == set(d["by_kind"]) and d["plan"]["min_join_rate_pct"] == 94.5
    assert len(d["sample"]) == 3 and all((s["ev_id"] or "PE-").startswith("PE-") for s in d["sample"])


def test_weekly_monitor_compares_charge_only_when_driving_not_planned():
    ev = _ev()
    w = kpi.weekly_monitor(ev, _ch().iloc[0:0])
    row = w["rows"][-1]
    assert row["obd"] == 1 and row["obd_drive"] == 1 and "운행" in w["obd_basis"]
