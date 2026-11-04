"""공개 데이터 CSV(시간별 전력수요·재생에너지) 반영 — 가로형·세로형 모두 읽는다."""
import pandas as pd
import pytest

from src.pipeline import public_csv as pc


def test_wide_format_1_to_24_hours_cp949():
    head = "날짜," + ",".join(f"{h}시" for h in range(1, 25))
    row = "2026-11-02," + ",".join(str(60000 + h) for h in range(1, 25))
    s = pc.parse_hourly((head + "\n" + row + "\n").encode("cp949"))
    assert len(s) == 24
    assert s.iloc[0].tolist() == [pd.Timestamp("2026-11-02 00:00"), 60001]       # 1시 = 00~01시 구간
    assert s.iloc[-1].tolist() == [pd.Timestamp("2026-11-02 23:00"), 60024]


def test_long_format_filters_region_and_sums_fuel_types():
    csv = ("거래일자,거래시간,지역,연료원,전력거래량(MWh)\n"
           "20261102,1,전라남도,태양광,0\n20261102,13,전라남도,태양광,\"1,200.5\"\n20261102,13,전라남도,풍력,99.5\n"
           "20261102,13,경상북도,태양광,800\n")
    s = pc.parse_hourly(csv.encode("utf-8"), region="전라남도")
    assert dict(zip(s["timestamp"].dt.hour, s["value"])) == {12: 1300.0, 0: 0.0}   # 13시 = 12~13시 구간, 연료원 합계


def test_unreadable_shapes_raise():
    with pytest.raises(pc.CsvFormatError):
        pc.parse_hourly("a,b\n1,2\n".encode())
    with pytest.raises(pc.CsvFormatError, match="지역"):
        pc.parse_hourly("일자,시간,지역,발전량\n20261102,1,서울,5\n".encode(), region="전남")


def test_merge_overwrites_matching_hours_only():
    grid = pd.DataFrame({"timestamp": pd.date_range("2026-11-02", periods=48, freq="h"), "power_demand_mw": 1.0})
    series = pd.DataFrame({"timestamp": [pd.Timestamp("2026-11-02 05:00"), pd.Timestamp("2026-12-01")], "value": [77.0, 5.0]})
    merged, n = pc.merge_into_grid(grid, series, "power_demand_mw")
    assert n == 1 and merged.loc[5, "power_demand_mw"] == 77.0 and merged["power_demand_mw"].sum() == 47 + 77


def test_upload_endpoint_validates_kind_and_format(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from src.api import app
    from src.auth import deps
    from src.pipeline import source
    monkeypatch.setattr(source, "GRID_CSV", tmp_path / "grid_signal.csv")
    monkeypatch.setattr(source, "EV_CSV", tmp_path / "ev.csv")
    app.dependency_overrides[deps.require_admin] = lambda: {"username": "t", "role": "admin"}
    try:
        c = TestClient(app)
        assert c.post("/ingest/public-csv/weather", files={"file": ("a.csv", b"x")}).status_code == 404
        assert c.post("/ingest/public-csv/power-demand", files={"file": ("a.csv", b"a,b\n1,2\n")}).status_code == 422
        head = "날짜," + ",".join(f"{h}시" for h in range(1, 25))
        body = (head + "\n2026-10-10," + ",".join("100" for _ in range(24)) + "\n").encode()
        r = c.post("/ingest/public-csv/power-demand", files={"file": ("d.csv", body)})
        assert r.status_code == 200 and r.json()["rows_in_file"] == 24 and (tmp_path / "grid_signal.csv").exists()
    finally:
        app.dependency_overrides.clear()
