"""재귀 예측(forecast_horizon) 테스트 — 일별(시간대별)/주별/월별 조회 단위.

GRANULARITY_PRESETS의 실제 horizon_hours(최대 4,320시간)는 회귀테스트 속도를 위해
필요한 곳만 작게 monkeypatch한다 — 로직(버킷팅, 재귀 피드백) 검증이 목적이지 실제
운영 규모 성능 측정이 목적이 아니다.
"""
import pytest

from src.automation.model_registry import ModelRegistry
from src.automation.scheduler import bootstrap
from src.datagen.config import SimConfig
from src.model import forecast_service

TARGET = "ev_charging_demand_kw"


@pytest.fixture
def trained_registry(tmp_path):
    reg = ModelRegistry(TARGET, base_dir=tmp_path)
    cfg = SimConfig(n_ev=5, weeks=6, n_chargers=5, charger_days=42, n_zones=2)
    bootstrap(reg, cfg, TARGET)
    return reg, cfg


def _patch_registry(monkeypatch, reg):
    monkeypatch.setattr(forecast_service, "ModelRegistry", lambda target_col: reg)


def test_forecast_horizon_hourly_returns_48_points(monkeypatch, trained_registry):
    reg, cfg = trained_registry
    _patch_registry(monkeypatch, reg)

    result = forecast_service.forecast_horizon(TARGET, "hourly", cfg=cfg)

    assert result["bucket"] == "hour"
    assert result["granularity"] == "hourly"
    assert len(result["timestamps"]) == 48
    assert len(result["values"]) == 48
    assert all(v >= 0 for v in result["values"])
    # 타임스탬프가 1시간 간격으로 단조 증가해야 함
    assert result["timestamps"] == sorted(result["timestamps"])


def test_forecast_horizon_daily_bucket_aggregates_fewer_points(monkeypatch, trained_registry):
    reg, cfg = trained_registry
    _patch_registry(monkeypatch, reg)
    monkeypatch.setitem(
        forecast_service.GRANULARITY_PRESETS, "daily", {"horizon_hours": 48, "bucket": "day", "label": "주별"}
    )

    result = forecast_service.forecast_horizon(TARGET, "daily", cfg=cfg)

    assert result["bucket"] == "day"
    assert 1 <= len(result["values"]) <= 3  # 48시간을 일 단위로 묶으면 최대 3개
    assert all(v >= 0 for v in result["values"])


def test_forecast_horizon_rejects_unknown_granularity(trained_registry):
    _reg, cfg = trained_registry
    with pytest.raises(ValueError):
        forecast_service.forecast_horizon(TARGET, "yearly", cfg=cfg)
