"""KPX SMP 연동 테스트.

실제 KPX 인증키가 없어(공공데이터포털 심의승인 대기) 라이브 호출은 검증하지
못했다 — `parse_smp_xml`/`merge_smp_into_grid_signal`은 순수 함수라 data.go.kr에
문서화된 응답 필드 명세로 만든 샘플 XML로 검증하고, 네트워크 호출이 필요한
`fetch_smp_hourly`는 "키 없으면 즉시 실패" 경로만 검증한다.
"""
import pandas as pd
import pytest

from src.pipeline.kpx_ingest import (
    KpxApiError,
    KpxNotConfigured,
    fetch_smp_hourly,
    merge_smp_into_grid_signal,
    parse_smp_xml,
)

SAMPLE_SUCCESS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header>
    <resultCode>00</resultCode>
    <resultMsg>OK</resultMsg>
  </header>
  <body>
    <items>
      <item>
        <tradeDay>20260917</tradeDay>
        <tradHour>0</tradHour>
        <areaCd>1</areaCd>
        <smp>93.47</smp>
      </item>
      <item>
        <tradeDay>20260917</tradeDay>
        <tradHour>1</tradHour>
        <areaCd>1</areaCd>
        <smp>91.20</smp>
      </item>
    </items>
    <pageNo>1</pageNo>
    <numOfRows>1000</numOfRows>
    <totalCount>2</totalCount>
  </body>
</response>
"""

SAMPLE_ERROR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<response>
  <header>
    <resultCode>30</resultCode>
    <resultMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</resultMsg>
  </header>
</response>
"""


def test_parse_smp_xml_success():
    df = parse_smp_xml(SAMPLE_SUCCESS_XML)
    assert list(df.columns) == ["timestamp", "price_krw_per_kwh"]
    assert len(df) == 2
    assert df.iloc[0]["timestamp"] == pd.Timestamp("2026-09-17 00:00:00")
    assert df.iloc[0]["price_krw_per_kwh"] == 93.47
    assert df.iloc[1]["timestamp"] == pd.Timestamp("2026-09-17 01:00:00")


def test_parse_smp_xml_raises_on_error_result_code():
    with pytest.raises(KpxApiError, match="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"):
        parse_smp_xml(SAMPLE_ERROR_XML)


def test_fetch_smp_hourly_requires_service_key(monkeypatch):
    monkeypatch.delenv("KPX_SERVICE_KEY", raising=False)
    with pytest.raises(KpxNotConfigured):
        fetch_smp_hourly()


def test_fetch_smp_hourly_rejects_unknown_area(monkeypatch):
    monkeypatch.setenv("KPX_SERVICE_KEY", "dummy-key-not-called")
    with pytest.raises(ValueError):
        fetch_smp_hourly(area="busan")


def test_merge_smp_into_grid_signal_overwrites_matching_hours_only():
    grid_df = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-17", periods=3, freq="h"),
        "power_demand_mw": [100.0, 110.0, 120.0],
        "renewable_gen_mw": [10.0, 20.0, 30.0],
        "temperature_c": [20.0, 20.5, 21.0],
        "price_krw_per_kwh": [100.0, 100.0, 100.0],  # 합성값(전부 동일) — 실측으로 덮어써야 함
        "is_holiday": [0, 0, 0],
    })
    smp_df = pd.DataFrame({
        "timestamp": pd.date_range("2026-09-17", periods=2, freq="h"),  # 앞 2시간만 실측 있음
        "price_krw_per_kwh": [93.47, 91.20],
    })

    merged = merge_smp_into_grid_signal(grid_df, smp_df)

    assert merged.loc[0, "price_krw_per_kwh"] == 93.47
    assert merged.loc[1, "price_krw_per_kwh"] == 91.20
    assert merged.loc[2, "price_krw_per_kwh"] == 100.0  # 실측 없는 시간대는 합성값 유지
    # SMP는 다른 필드를 만들 근거가 없으므로 건드리지 않아야 함
    assert (merged["power_demand_mw"] == grid_df["power_demand_mw"]).all()
    assert (merged["renewable_gen_mw"] == grid_df["renewable_gen_mw"]).all()
