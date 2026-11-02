"""DR(수요반응) 이벤트 — 등록/조회/가격신호 반영 테스트."""
import pandas as pd
import pytest

from src.grid import dr_events


@pytest.fixture(autouse=True)
def _clear():
    dr_events.clear_dr_events()
    yield
    dr_events.clear_dr_events()


def test_create_and_list_dr_event():
    event_id = dr_events.create_dr_event(
        pd.Timestamp("2026-09-17 18:00"), pd.Timestamp("2026-09-17 20:00"), severity="high", reason="동계 피크"
    )
    events = dr_events.list_dr_events()
    assert len(events) == 1
    assert events[0]["event_id"] == event_id
    assert events[0]["severity"] == "high"
    assert events[0]["reason"] == "동계 피크"


def test_create_dr_event_rejects_unknown_severity():
    with pytest.raises(ValueError):
        dr_events.create_dr_event(pd.Timestamp("2026-09-17 18:00"), pd.Timestamp("2026-09-17 20:00"), severity="extreme")


def test_create_dr_event_rejects_end_before_start():
    with pytest.raises(ValueError):
        dr_events.create_dr_event(pd.Timestamp("2026-09-17 20:00"), pd.Timestamp("2026-09-17 18:00"))


def test_delete_dr_event():
    event_id = dr_events.create_dr_event(pd.Timestamp("2026-09-17 18:00"), pd.Timestamp("2026-09-17 20:00"))
    dr_events.delete_dr_event(event_id)
    assert dr_events.list_dr_events() == []


def test_delete_unknown_dr_event_raises():
    with pytest.raises(ValueError):
        dr_events.delete_dr_event("DR-nonexistent")


def test_apply_dr_premium_only_affects_active_window():
    times = pd.date_range("2026-09-17 17:00", periods=5, freq="h")
    price_by_time = {t: 100.0 for t in times}
    dr_events.create_dr_event(times[2], times[4], severity="high")  # 19시~21시(=index 2,3) 커버

    adjusted = dr_events.apply_dr_premium_to_price(price_by_time)

    assert adjusted[times[0]] == 100.0  # DR 이전 — 그대로
    assert adjusted[times[1]] == 100.0  # DR 이전 — 그대로
    assert adjusted[times[2]] == 100.0 * dr_events.SEVERITY_MULTIPLIER["high"]  # DR 활성
    assert adjusted[times[3]] == 100.0 * dr_events.SEVERITY_MULTIPLIER["high"]  # DR 활성
    assert adjusted[times[4]] == 100.0  # end_ts는 배타적 경계 — DR 아님


def test_apply_dr_premium_uses_highest_overlapping_severity():
    times = pd.date_range("2026-09-17 18:00", periods=1, freq="h")
    price_by_time = {times[0]: 100.0}
    dr_events.create_dr_event(times[0], times[0] + pd.Timedelta(hours=1), severity="low")
    dr_events.create_dr_event(times[0], times[0] + pd.Timedelta(hours=1), severity="high")

    adjusted = dr_events.apply_dr_premium_to_price(price_by_time)
    assert adjusted[times[0]] == 100.0 * dr_events.SEVERITY_MULTIPLIER["high"]


def test_apply_dr_premium_noop_when_no_events():
    times = pd.date_range("2026-09-17 18:00", periods=2, freq="h")
    price_by_time = {t: 100.0 for t in times}
    assert dr_events.apply_dr_premium_to_price(price_by_time) == price_by_time
