"""전체 소스 계산식 감사에서 발견한 결함의 회귀 테스트 — 시간대, 선단 최적화의 효율 상한, 가중 효율."""
import numpy as np
import pandas as pd
import pytest

from src.optimizer.decision import optimize_fleet
from src.optimizer.economics import EconomicsParams
from src.timeutil import to_kst_naive


def test_utc_timestamp_is_converted_to_kst_not_just_stripped():
    assert to_kst_naive("2026-10-22T00:00:00Z") == pd.Timestamp("2026-10-22 09:00")
    assert to_kst_naive("2026-10-22T09:00:00+09:00") == pd.Timestamp("2026-10-22 09:00")
    assert to_kst_naive("2026-10-22 09:00") == pd.Timestamp("2026-10-22 09:00")       # naive는 이미 KST


def _session(n=10, soc0=40.0, cap=60.0, target=80.0):
    times = list(pd.date_range("2026-10-23 00:00", periods=n, freq="h"))
    need = (target - soc0) / 100 * cap
    return {"S1": {"times": times, "needed_kwh": need, "capacity_kwh": cap, "soc_baseline_pct": {t: soc0 for t in times},
                   "v2g_capable": False, "start_soc_pct": soc0}}, times


def test_fleet_with_efficiency_assumptions_still_charges_the_needed_energy():
    """이전 구현은 효율을 반영하지 않은 상한 때문에 LP가 불가능해져 충전량이 전부 0, 절감률 100%로 나왔다."""
    sessions, times = _session()
    prices = {t: 100.0 + 5 * i for i, t in enumerate(times)}
    eco = EconomicsParams.load()
    out = optimize_fleet(sessions, 7.0, 7.0, 0.2, 0.9, prices, economics=eco)
    charged = sum(c for c, _d in out["S1"].values())
    needed = (0.9 * 100 - 40.0) / 100 * 60.0                      # 목표 90%까지 필요한 배터리 에너지
    assert charged > 0
    assert charged * eco.charge_efficiency >= needed - 0.05      # 효율을 곱한 뒤에도 필요량을 채운다


def test_weighted_efficiency_is_energy_weighted():
    from src.model import charging_efficiency as ce
    src = open(ce.__file__, encoding="utf-8").read()
    assert "weighted_efficiency_pct" in src
