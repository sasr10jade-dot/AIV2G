"""공공데이터 API 커넥터(설정 기반 파싱)와 전남 실측 발전량 기반 날씨 보정 — 실제 호출 없이 가짜 응답으로 검증."""
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.pipeline import public_api as api
from src.solar import pv_model, regional
from src.solar.emulator import EmulationError, run_emulation
from src.solar.site import get_site

client = TestClient(app)
SITE = get_site("naju_solar_ev")
SPEC = api.load_spec("kpx_solar_regional")
FILLED = api.ApiSpec(**{**SPEC.__dict__, "endpoint": "https://example.test/solar"})   # 명세가 채워졌다고 가정


def _json(items, code="00"):
    return json.dumps({"response": {"header": {"resultCode": code}, "body": {"items": {"item": items}}}}, ensure_ascii=False)


def _xml(items, code="00"):
    body = "".join(f"<item><date>{i['date']}</date><hour>{i['hour']}</hour><region>{i['region']}</region><mwh>{i['mwh']}</mwh></item>"
                   for i in items)
    return f"<response><header><resultCode>{code}</resultCode></header><body><items>{body}</items></body></response>"


ROWS = [
    {"date": "20260615", "hour": "1", "region": "전라남도", "mwh": "0"},
    {"date": "20260615", "hour": "13", "region": "전라남도", "mwh": "1,234.5"},
    {"date": "20260615", "hour": "13", "region": "경상북도", "mwh": "900"},
]


# --------------------------------------------------------------- 설정·상태 --

def test_default_spec_is_honest_about_what_is_unverified(monkeypatch):
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    st = SPEC.status()
    assert st["endpoint_configured"] is False and st["spec_verified"] is False and st["key_configured"] is False
    assert "전남" in st["granularity"] and st["unit"] == "MWh"
    assert "KPX_SERVICE_KEY" == st["key_env"]
    assert SPEC.endpoint is None                       # 확인되지 않은 주소를 지어내지 않았다


def test_status_never_exposes_the_key(monkeypatch):
    monkeypatch.setenv("KPX_SERVICE_KEY", "secret-key-123")
    assert "secret-key-123" not in json.dumps(SPEC.status())
    assert SPEC.status()["key_configured"] is True


def test_unknown_api_id():
    with pytest.raises(KeyError):
        api.load_spec("nope")


def test_fetch_refuses_without_key_or_endpoint_and_does_not_call_network(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            pytest.fail("네트워크를 호출하면 안 된다")
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    with pytest.raises(api.ApiNotConfigured, match="KPX_SERVICE_KEY"):
        api.fetch_day(FILLED, "2026-06-15", session=Boom())
    with pytest.raises(api.ApiNotConfigured, match="엔드포인트"):
        api.fetch_day(SPEC, "2026-06-15", service_key="k", session=Boom())


# -------------------------------------------------------------------- 파싱 --

@pytest.mark.parametrize("make", [_json, _xml])
def test_json_and_xml_parse_to_the_same_frame(make):
    frame = api.items_to_frame(api.parse_items(make(ROWS), SPEC), SPEC)
    assert list(frame.columns) == ["timestamp", "region", "value"] and len(frame) == 3
    jn = frame[(frame.region == "전라남도") & (frame.timestamp.dt.hour == 12)]
    assert jn["value"].iloc[0] == pytest.approx(1234.5)             # 쉼표 제거
    assert frame["timestamp"].min() == pd.Timestamp("2026-06-15 00:00")   # 거래시간 1 → 00시(hour_base=1)


def test_single_item_response_is_handled_as_a_list():
    assert len(api.parse_items(_json(ROWS[0]), SPEC)) == 1
    assert len(api.parse_items(_xml(ROWS[:1]), SPEC)) == 1


def test_empty_and_missing_items():
    assert api.parse_items(_json([]), SPEC) == [] or api.parse_items(_json(""), SPEC) == []
    assert api.parse_items(json.dumps({"response": {"header": {"resultCode": "00"}, "body": {}}}), SPEC) == []


@pytest.mark.parametrize("payload", [_json(ROWS, "22"), _xml(ROWS, "30"), json.dumps({"response": {"body": {}}})])
def test_error_codes_raise(payload):
    with pytest.raises(api.ApiResponseError):
        api.parse_items(payload, SPEC)


@pytest.mark.parametrize("payload", ["not json or xml", "{bad json", "<unclosed>", ""])
def test_undecodable_responses_raise(payload):
    with pytest.raises(api.ApiResponseError):
        api.parse_items(payload, SPEC)


def test_wrong_field_mapping_gives_a_helpful_error():
    items = [{"일자": "20260615", "시간": "1", "지역": "전라남도", "발전량": "1"}]
    with pytest.raises(api.ApiResponseError, match="필드 매핑"):
        api.items_to_frame(items, SPEC)


def test_non_numeric_value_becomes_nan_not_a_crash():
    frame = api.items_to_frame([{"date": "20260615", "hour": "5", "region": "전라남도", "mwh": "-"}], SPEC)
    assert np.isnan(frame["value"].iloc[0])


def test_bom_prefixed_json_is_accepted():
    assert len(api.parse_items(("﻿" + _json(ROWS)).encode("utf-8"), SPEC)) == 3


# ---------------------------------------------------------------- 호출·페이지 --

class FakeResp:
    def __init__(self, content):
        self.content = content.encode("utf-8")

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params)))
        return FakeResp(self.pages[len(self.calls) - 1])


def test_fetch_day_sends_configured_params_and_paginates():
    rows = [{"date": "20260615", "hour": str(h), "region": "전라남도", "mwh": str(h)} for h in range(1, 6)]
    sess = FakeSession([_json(rows[:2]), _json(rows[2:4]), _json(rows[4:])])
    frame = api.fetch_day(FILLED, "2026-06-15", service_key="K", page_size=2, session=sess)
    assert len(frame) == 5 and len(sess.calls) == 3
    url, params = sess.calls[0]
    assert url == "https://example.test/solar"
    assert params["serviceKey"] == "K" and params["date"] == "20260615" and params["pageNo"] == 1 and params["numOfRows"] == 2
    assert [c[1]["pageNo"] for c in sess.calls] == [1, 2, 3]


def test_fetch_day_stops_on_empty_and_caps_runaway_paging():
    assert len(api.fetch_day(FILLED, "2026-06-15", service_key="K", session=FakeSession([_json([])]))) == 0
    endless = FakeSession([_json([ROWS[0]] * 2)] * 60)
    with pytest.raises(api.ApiResponseError, match="페이지"):
        api.fetch_day(FILLED, "2026-06-15", service_key="K", page_size=2, session=endless)


def test_http_errors_propagate():
    class Bad(FakeResp):
        def raise_for_status(self):
            raise RuntimeError("HTTP 500")
    class S:
        def get(self, *a, **k):
            return Bad("{}")
    with pytest.raises(RuntimeError):
        api.fetch_day(FILLED, "2026-06-15", service_key="K", session=S())


# --------------------------------------------------------------- 전남 발전량 --

def _fake_fetcher(true_factor_by_day, k_mw=3000.0, other_region=True):
    """정답 날씨 계수로 전남 발전량(MWh)을 만들어 주는 가짜 fetch_day."""
    def fetch(spec, day, **kw):
        d = pd.Timestamp(day).normalize()
        idx = pd.date_range(d, periods=24, freq="h")
        clear = pv_model.clear_sky_ghi(idx, SITE).to_numpy()
        gen = k_mw * clear / 1000.0 * true_factor_by_day[d]
        rows = [{"timestamp": t, "region": "전라남도", "value": float(g)} for t, g in zip(idx, gen)]
        if other_region:
            rows += [{"timestamp": t, "region": "경상북도", "value": 5000.0} for t in idx]
        return pd.DataFrame(rows)
    return fetch


def _truth(days=10):
    start = pd.Timestamp("2026-06-01")
    # 절반은 맑은 날(0.95)이라 95퍼센타일이 맑은 날 기준과 일치한다
    pattern = [0.95, 0.4, 0.95, 0.7, 0.95, 0.15, 0.95, 0.55, 0.95, 0.3]
    return {start + pd.Timedelta(days=i): pattern[i % len(pattern)] for i in range(days)}


def test_fetch_filters_to_jeonnam_and_sums_duplicates():
    truth = _truth(2)
    gen = regional.fetch_regional_generation("2026-06-01", "2026-06-02", fetcher=_fake_fetcher(truth))
    assert gen.index.min() == pd.Timestamp("2026-06-01 00:00") and len(gen) == 48
    assert gen.max() < 5000                                               # 경북(5000)이 섞이지 않았다


def test_fetch_window_limits_and_no_data():
    with pytest.raises(regional.CalibrationError, match="너무 깁니다"):
        regional.fetch_regional_generation("2026-01-01", "2026-12-31", fetcher=_fake_fetcher({}))
    with pytest.raises(regional.CalibrationError, match="빠릅니다"):
        regional.fetch_regional_generation("2026-06-05", "2026-06-01", fetcher=_fake_fetcher({}))
    with pytest.raises(regional.CalibrationError, match="없습니다"):
        regional.fetch_regional_generation("2026-06-01", "2026-06-02", fetcher=lambda s, d, **k: pd.DataFrame(columns=["timestamp", "region", "value"]))


def test_calibration_recovers_the_true_weather_from_regional_generation():
    truth = _truth(10)
    gen = regional.fetch_regional_generation("2026-06-01", "2026-06-10", fetcher=_fake_fetcher(truth))
    idx = pd.date_range("2026-06-01", periods=24 * 10, freq="h")
    factor = regional.calibrate_sky_factor(gen, idx, SITE)
    clear = pv_model.clear_sky_ghi(idx, SITE).to_numpy()
    expected = np.array([truth[t.normalize()] for t in idx])
    mask = clear > 200
    assert np.abs(factor.to_numpy()[mask] - expected[mask]).max() < 0.03   # 용량(3000MW)을 몰라도 날씨 영향을 복원한다


def test_calibration_fills_gaps_with_day_mean_and_climatology():
    truth = _truth(8)
    gen = regional.fetch_regional_generation("2026-06-01", "2026-06-08", fetcher=_fake_fetcher(truth))
    gen = gen.drop(gen.index[(gen.index >= "2026-06-03 11:00") & (gen.index <= "2026-06-03 13:00")])   # 한낮 3시간 결측
    gen = gen[gen.index.normalize() != pd.Timestamp("2026-06-05")]                                       # 하루 전체 결측
    idx = pd.date_range("2026-06-01", periods=24 * 8, freq="h")
    f = regional.calibrate_sky_factor(gen, idx, SITE)
    assert not f.isna().any() and f.between(0, 1).all()
    assert f["2026-06-03 12:00"] == pytest.approx(0.95, abs=0.03)           # 그날 평균 계수로 보간(그날은 맑음)
    assert f["2026-06-05"].eq(pv_model.CLIMATOLOGY_SKY_FACTOR).all()        # 관측이 전혀 없는 날은 평균 날씨


def test_calibration_needs_enough_days_and_nonzero_data():
    gen = regional.fetch_regional_generation("2026-06-01", "2026-06-03", fetcher=_fake_fetcher(_truth(3)))
    with pytest.raises(regional.CalibrationError, match="부족"):
        regional.calibrate_sky_factor(gen, pd.date_range("2026-06-01", periods=72, freq="h"), SITE)
    zero = pd.Series(0.0, index=pd.date_range("2026-06-01", periods=24 * 10, freq="h"))
    with pytest.raises(regional.CalibrationError):
        regional.calibrate_sky_factor(zero, zero.index, SITE)


def test_period_helper_extends_window_for_short_runs_and_rejects_future():
    truth = {pd.Timestamp("2026-06-01") + pd.Timedelta(days=i): 0.95 if i % 2 == 0 else 0.5 for i in range(20)}
    calls = []
    base = _fake_fetcher(truth)

    def fetcher(spec, day, **kw):
        calls.append(pd.Timestamp(day).normalize())
        return base(spec, day)
    factor = regional.sky_factor_for_period("2026-06-10", 2, SITE, fetcher=fetcher, today="2026-06-20")
    assert len(factor) == 2 * 24 + 14 and factor.index[0] == pd.Timestamp("2026-06-10")
    assert min(calls) < pd.Timestamp("2026-06-10")                          # 맑은 날 기준용으로 앞쪽을 더 조회
    with pytest.raises(regional.CalibrationError, match="어제까지"):
        regional.sky_factor_for_period("2026-06-19", 2, SITE, fetcher=fetcher, today="2026-06-20")


# --------------------------------------------------------------- 에뮬레이터 --

def test_emulator_uses_injected_real_weather():
    idx_len = 24 * 2 + 14
    idx = pd.date_range("2026-06-15", periods=idx_len, freq="h")
    cloudy = pd.Series(0.3, index=idx)
    base = run_emulation(SITE, "2026-06-15", days=2, seed=1)
    real = run_emulation(SITE, "2026-06-15", days=2, seed=1, sky_factor=cloudy, weather_source="kpx_regional")
    assert real["weather_source"] == "kpx_regional" and base["weather_source"] == "model"
    assert real["baseline"]["pv_generation_kwh"] < base["baseline"]["pv_generation_kwh"] * 0.6
    assert real["daily"][0]["sky"] == "cloudy"
    with pytest.raises(EmulationError, match="덮지"):
        run_emulation(SITE, "2026-06-15", days=2, sky_factor=cloudy.iloc[:10])


# ---------------------------------------------------------------------- API --

@pytest.fixture
def admin(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("opuser", "op-password-1", "admin")
    client.post("/auth/login", json={"username": "opuser", "password": "op-password-1"})
    yield
    client.post("/auth/logout")


def test_data_sources_endpoint_reports_readiness(admin, monkeypatch):
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    s = client.get("/solar/data-sources").json()["sources"][0]
    assert s["api_id"] == "kpx_solar_regional" and s["endpoint_configured"] is False and s["spec_verified"] is False


def test_emulate_with_real_weather_without_setup_gives_a_clear_503(admin, monkeypatch):
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    r = client.post("/solar/emulate", json={"start_date": "2026-06-01", "days": 2, "weather_source": "kpx_regional"})
    assert r.status_code == 503 and ("KPX_SERVICE_KEY" in r.json()["detail"] or "엔드포인트" in r.json()["detail"])


def test_emulate_with_real_weather_future_dates_rejected(admin):
    r = client.post("/solar/emulate", json={"start_date": "2099-06-01", "days": 2, "weather_source": "kpx_regional"})
    assert r.status_code == 422 and "어제까지" in r.json()["detail"]


def test_emulate_with_real_weather_end_to_end(admin, monkeypatch):
    truth = {pd.Timestamp("2026-06-01") + pd.Timedelta(days=i): 0.95 if i % 2 == 0 else 0.4 for i in range(30)}
    original = regional.sky_factor_for_period
    monkeypatch.setattr(
        regional, "sky_factor_for_period",
        lambda start, days, site, **kw: original(start, days, site, fetcher=_fake_fetcher(truth), today="2026-07-15"))
    r = client.post("/solar/emulate", json={"start_date": "2026-06-10", "days": 3, "weather_source": "kpx_regional"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["weather_source"] == "kpx_regional" and body["verified"] is False
    # 정답 날씨: 6/1부터 짝수번째 날=맑음(0.95), 홀수번째 날=흐림(0.4) → 6/10은 9번째라 흐림, 6/11 맑음, 6/12 흐림
    assert [d["sky"] for d in body["daily"]] == ["cloudy", "clear", "cloudy"]
    assert body["daily"][1]["pv_kwh"] > 1.8 * body["daily"][0]["pv_kwh"]


def test_invalid_weather_source_rejected(admin):
    assert client.post("/solar/emulate", json={"weather_source": "psychic"}).status_code == 422
