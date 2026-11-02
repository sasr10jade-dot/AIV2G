"""차량 OBD 충전량 vs 충전기 계측 충전량 비교(충전 효율 추정) 테스트."""
import numpy as np
import pandas as pd
import pytest

from src.datagen.charger import CHARGER_EFFICIENCY, generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.model.charging_efficiency import NoLinkedSessionsError, compute_charging_efficiency


def test_compute_charging_efficiency_recovers_synthetic_efficiency():
    """합성 데이터의 charger.py는 CHARGER_EFFICIENCY(0.92)로 나눠 energy_kwh를
    만드므로, 역산한 평균 효율이 그 값 근처(±5%p)로 나와야 한다 — "차량-충전기
    데이터를 연결해 효율을 추정하는 방법 자체가 동작한다"는 핵심 검증."""
    cfg = SimConfig(n_ev=20, weeks=6, n_chargers=15, charger_days=42, n_zones=2)
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    charger_df = generate_charger_sessions(cfg, rng, ev_df=ev_df)

    result = compute_charging_efficiency(ev_df, charger_df)

    assert result["n_sessions_analyzed"] > 0
    assert abs(result["avg_efficiency_pct"] - CHARGER_EFFICIENCY * 100) < 5.0
    assert "note" in result


def test_compute_charging_efficiency_no_ev_id_column_raises():
    ev_df = pd.DataFrame({"ev_id": [], "timestamp": [], "charging_kw": []})
    charger_df = pd.DataFrame({"session_id": [], "charger_id": [], "energy_kwh": [], "start_ts": [], "duration_h": []})
    with pytest.raises(NoLinkedSessionsError):
        compute_charging_efficiency(ev_df, charger_df)


def test_compute_charging_efficiency_all_null_ev_id_raises():
    ev_df = pd.DataFrame({"ev_id": ["EV-001"], "timestamp": [pd.Timestamp("2026-01-01")], "charging_kw": [5.0]})
    charger_df = pd.DataFrame({
        "session_id": ["S1"], "charger_id": ["CHG-0001"], "energy_kwh": [5.0],
        "start_ts": [pd.Timestamp("2026-01-01")], "duration_h": [1.0], "ev_id": [None],
    })
    with pytest.raises(NoLinkedSessionsError):
        compute_charging_efficiency(ev_df, charger_df)
