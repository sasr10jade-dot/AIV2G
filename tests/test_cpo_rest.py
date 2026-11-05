"""충전사업자 REST API 어댑터 — 가짜 응답으로 페이지·인증·필드 매핑·선택 컬럼·동기화를 검증(실제 호출 없음)."""
import json
import sqlite3

import pytest

from src.cpo.adapters import RestApiAdapter, load_adapters
from src.cpo.base import CpoDataError
from src.cpo.sync import sync_all

MAP = {"session_id": "txnId", "charger_id": "chargerId", "start_ts": "startTime", "end_ts": "endTime",
       "energy_kwh": "energyKwh", "connector_type": "connectorType", "stop_reason": "stopReason", "error_code": "errorCode"}


def _item(i, start="2027-01-10T01:00:00", end="2027-01-10T03:30:00"):
    return {"txnId": f"T{i}", "chargerId": f"C{i % 3}", "startTime": start, "endTime": end, "energyKwh": 12.5,
            "connectorType": "AC_TYPE1", "stopReason": "EVDisconnected", "errorCode": None}


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return FakeResp({"data": {"items": self.pages[len(self.calls) - 1]}})


def _adapter(pages, **kw):
    http = FakeHttp(pages)
    return RestApiAdapter("CPO-A", "https://cpo.example/sessions", MAP, items_path="data.items", auth_env="CPO_A_API_TOKEN",
                          page_size=2, session=http, **kw), http


def test_paginates_sends_auth_and_maps_optional_columns(monkeypatch):
    monkeypatch.setenv("CPO_A_API_TOKEN", "tok")
    a, http = _adapter([[_item(1), _item(2)], [_item(3)]])
    df = a.fetch_sessions(since="2027-01-01")
    assert len(df) == 3 and len(http.calls) == 2                 # 2쪽째가 page_size보다 작아 멈춤
    url, params, headers = http.calls[0]
    assert headers == {"Authorization": "Bearer tok"} and params["since"] == "2027-01-01" and params["page"] == 1
    assert df["duration_h"].iloc[0] == 2.5                         # 종료 시각으로 충전 시간 계산
    assert {"end_ts", "connector_type", "stop_reason", "error_code"} <= set(df.columns)


def test_missing_token_and_missing_fields_fail_loudly(monkeypatch):
    monkeypatch.delenv("CPO_A_API_TOKEN", raising=False)
    a, _ = _adapter([[_item(1)]])
    with pytest.raises(CpoDataError, match="환경변수"):
        a.fetch_sessions()
    monkeypatch.setenv("CPO_A_API_TOKEN", "tok")
    bad = _item(1)
    del bad["energyKwh"]
    a, _ = _adapter([[bad]])
    with pytest.raises(CpoDataError, match="energyKwh"):
        a.fetch_sessions()


def test_runaway_paging_is_capped(monkeypatch):
    monkeypatch.setenv("CPO_A_API_TOKEN", "tok")
    a, _ = _adapter([[_item(1), _item(2)]] * 5, max_pages=3)
    with pytest.raises(CpoDataError, match="페이지"):
        a.fetch_sessions()


def test_sync_stores_optional_columns_idempotently(monkeypatch):
    monkeypatch.setenv("CPO_A_API_TOKEN", "tok")
    con = sqlite3.connect(":memory:")
    for _ in range(2):   # 같은 세션을 두 번 받아도 한 번만 남는다
        a, _ = _adapter([[_item(1), _item(2)], []])
        assert sync_all([a], con=con)["ok"] is True
    rows = con.execute("SELECT session_id, operator_id, source, stop_reason, connector_type FROM charger_sessions ORDER BY session_id").fetchall()
    assert rows == [("CPO-A:T1", "CPO-A", "cpo_api", "EVDisconnected", "AC_TYPE1"),
                    ("CPO-A:T2", "CPO-A", "cpo_api", "EVDisconnected", "AC_TYPE1")]


def test_config_builds_rest_adapter(tmp_path):
    cfg = {"sources": [{"operator_id": "CPO-X", "type": "rest", "enabled": True, "url": "https://x", "column_map": MAP,
                        "items_path": "items", "page_size": 100}]}
    p = tmp_path / "cpo.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    (a,) = load_adapters(p)
    assert isinstance(a, RestApiAdapter) and a.page_size == 100 and a.items_path == "items"


def test_shipped_config_lists_three_operators_all_disabled():
    import src.cpo.adapters as ad
    data = json.loads(ad.CPO_CONFIG.read_text(encoding="utf-8"))
    real = [s for s in data["sources"] if s["type"] != "mock"]
    assert [s["operator_id"] for s in real] == ["niceinfra", "pluglink", "evsis"]
    assert load_adapters() == []                                   # 용역 체결 전에는 모두 꺼져 있다
