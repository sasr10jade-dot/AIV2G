"""공공데이터 API 커넥터(설정 기반 파싱) — 실제 호출 없이 가짜 응답으로 검증."""
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.pipeline import public_api as api

client = TestClient(app)
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
