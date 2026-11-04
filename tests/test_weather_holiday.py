"""기상·공휴일 공개 데이터 수집(계획서 4.1 가 외부환경) — 네트워크 없이 응답을 흉내 내 검증한다."""
import json

import pandas as pd
import pytest

from src.pipeline import weather_holiday as wh
from src.pipeline.public_api import ApiNotConfigured


class _Resp:
    def __init__(self, obj):
        self.content = json.dumps(obj).encode("utf-8")

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return _Resp(self.pages.pop(0))


def _body(items):
    return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": items} if items else ""}}}


def test_temperature_parses_hourly_items_and_drops_blanks():
    s = _Session([_body([{"tm": "2026-11-02 00:00", "ta": "8.1"}, {"tm": "2026-11-02 01:00", "ta": ""},
                         {"tm": "2026-11-02 02:00", "ta": "7.4"}])])
    df = wh.fetch_hourly_temperature("2026-11-02", "2026-11-02", service_key="k", session=s)
    assert list(df["temperature_c"]) == [8.1, 7.4]
    q = s.calls[0]
    assert q["stnIds"] == "156" and q["dataCd"] == "ASOS" and q["dateCd"] == "HR" and q["startDt"] == "20261102"


def test_holidays_queries_each_month_and_keeps_only_holidays():
    s = _Session([_body([{"locdate": 20261225, "dateName": "기독탄신일", "isHoliday": "Y"}]),
                  _body([{"locdate": 20270101, "dateName": "1월1일", "isHoliday": "Y"},
                         {"locdate": 20270102, "dateName": "기념일", "isHoliday": "N"}])])
    df = wh.fetch_holidays("2026-12-20", "2027-01-05", service_key="k", session=s)
    assert [c["solMonth"] for c in s.calls] == ["12", "01"]
    assert [d.isoformat() for d in df["date"]] == ["2026-12-25", "2027-01-01"]


def test_missing_key_raises_without_network(monkeypatch):
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    with pytest.raises(ApiNotConfigured):
        wh.fetch_hourly_temperature("2026-11-02", "2026-11-02", session=_Session([]))


def test_merge_overwrites_only_matching_rows():
    ts = pd.date_range("2026-12-24", periods=72, freq="h")
    grid = pd.DataFrame({"timestamp": ts, "temperature_c": 0.0, "is_holiday": 0})
    temp = pd.DataFrame({"timestamp": [ts[1]], "temperature_c": [-2.5]})
    hol = pd.DataFrame({"date": [pd.Timestamp("2026-12-25").date()], "name": ["기독탄신일"]})
    merged, stats = wh.merge_into_grid_signal(grid, temp, hol, "2026-12-24", "2026-12-25")
    assert merged.loc[1, "temperature_c"] == -2.5 and stats["temperature_rows"] == 1
    by_day = merged.groupby(merged["timestamp"].dt.date)["is_holiday"].max()
    assert by_day[pd.Timestamp("2026-12-24").date()] == 0 and by_day[pd.Timestamp("2026-12-25").date()] == 1
    assert by_day[pd.Timestamp("2026-12-26").date()] == 0          # 기간 밖(토요일이지만)은 그대로 둔다
    assert stats["holiday_rows"] == 48


def test_sync_endpoint_reports_503_without_key(monkeypatch):
    from fastapi.testclient import TestClient
    from src.api import app
    from src.auth import deps
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    app.dependency_overrides[deps.require_admin] = lambda: {"username": "t", "role": "admin"}
    try:
        c = TestClient(app)
        r = c.post("/ingest/sync-weather-holiday", params={"start": "2026-11-01", "end": "2026-11-02"})
        assert r.status_code == 503 and "DATA_GO_KR_SERVICE_KEY" in r.json()["detail"]
        assert c.post("/ingest/sync-weather-holiday", params={"start": "2026-11-01", "end": "2027-03-01"}).status_code == 422
        st = c.get("/ingest/public-apis/status").json()["apis"]
        assert {a["api_id"] for a in st} == {"kma_asos_hourly", "kasi_holidays"} and all(not a["key_configured"] for a in st)
    finally:
        app.dependency_overrides.clear()
