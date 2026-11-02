"""DR 에뮬레이터(자동 발생 스케줄러) 테스트."""
import asyncio

import pytest

from src.grid import dr_emulator, dr_events


@pytest.fixture(autouse=True)
def _cleanup():
    dr_events.clear_dr_events()
    yield
    dr_emulator.stop_emulator()
    dr_events.clear_dr_events()


def test_status_when_not_running():
    assert dr_emulator.status() == {"running": False, "config": None}


def test_start_emulator_creates_events_with_high_trigger_prob():
    """trigger_prob=1.0, interval 아주 짧게 하면 잠깐 기다린 뒤 이벤트가 반드시 생겨야 한다.

    pytest-asyncio가 없어(프로젝트 의존성에 없음) asyncio.run()으로 이벤트루프를 직접
    돌린다 — start_emulator 자체는 동기 함수(내부에서 asyncio.create_task만 함)라
    실행 중인 이벤트루프 안에서 짧게 sleep해줘야 백그라운드 태스크가 돌 시간이 생긴다."""
    async def _run():
        dr_emulator.start_emulator(interval_sec=0.05, trigger_prob=1.0, min_hours=1.0, max_hours=2.0)
        assert dr_emulator.status()["running"] is True
        await asyncio.sleep(0.3)  # interval 여러 번 지날 시간
        dr_emulator.stop_emulator()

    asyncio.run(_run())

    events = dr_events.list_dr_events()
    assert len(events) >= 1
    assert events[0]["severity"] in ("low", "medium", "high")
    assert events[0]["reason"] == "DR 에뮬레이터 자동 생성"


def test_start_emulator_is_idempotent_when_already_running():
    async def _run():
        dr_emulator.start_emulator(interval_sec=10.0, trigger_prob=0.0)
        first_task = dr_emulator._TASK
        dr_emulator.start_emulator(interval_sec=999.0, trigger_prob=0.0)  # 다시 호출해도 무시돼야 함
        assert dr_emulator._TASK is first_task

    asyncio.run(_run())


def test_stop_emulator_when_not_running_is_safe():
    dr_emulator.stop_emulator()  # 예외 없이 조용히 통과해야 함
    assert dr_emulator.status()["running"] is False
