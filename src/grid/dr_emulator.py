"""한전 계통 DR(수요반응) 신호 에뮬레이터 — 관리자가 매번 수동으로 이벤트를 등록하지
않아도, 데모/시연 중 자동으로 DR이 발생하는 것처럼 보여주기 위한 백그라운드 스케줄러.

**중요한 한계(OBD 에뮬레이터와의 차이)**: OBD 에뮬레이터는 실제 배치 업로드
인터페이스(`/ingest/upload-obd`, CSV 파일)를 거의 그대로 흉내내서 나중에 실제
데이터로 바꿔도 파이프라인 코드가 안 바뀐다. 반면 이 DR 에뮬레이터는 **실제 한전
DR 신호가 어떤 형태(웹훅? 폴링? EDI?)로 올지 확인된 바가 없어**, "미래의 실제
인터페이스를 미리 흉내낸 것"이 아니라 그냥 **이미 만들어둔 관리자용 등록 API
(`create_dr_event`)를 대신 호출해주는 타이머**일 뿐이다. 실제 한전 연동이
확정되면 이 스케줄러는 통째로 교체될 가능성이 높다 — 그때까지 데모/개발 편의용.
"""
from __future__ import annotations

import asyncio
import random

import pandas as pd

from src.timeutil import kst_now

from . import dr_events

_TASK: asyncio.Task | None = None
_running_config: dict | None = None

SEVERITIES = ["low", "medium", "high"]


async def _loop(interval_sec: float, trigger_prob: float, duration_hours_range: tuple[float, float]) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        if random.random() > trigger_prob:
            continue
        start = kst_now()  # 프로젝트 관례: 한국시간 naive(SimConfig 등과 동일). pd.Timestamp.now()는 서버 시간대를 따라 어긋날 수 있다
        duration_h = random.uniform(*duration_hours_range)
        end = start + pd.Timedelta(hours=duration_h)
        severity = random.choice(SEVERITIES)
        dr_events.create_dr_event(start, end, severity=severity, reason="DR 에뮬레이터 자동 생성")


def start_emulator(interval_sec: float = 60.0, trigger_prob: float = 0.3, min_hours: float = 1.0, max_hours: float = 4.0) -> None:
    """이미 실행 중이면 아무것도 하지 않는다(중복 시작 방지)."""
    global _TASK, _running_config
    if _TASK is not None and not _TASK.done():
        return
    _running_config = {
        "interval_sec": interval_sec, "trigger_prob": trigger_prob,
        "min_hours": min_hours, "max_hours": max_hours,
    }
    _TASK = asyncio.create_task(_loop(interval_sec, trigger_prob, (min_hours, max_hours)))


def stop_emulator() -> None:
    global _TASK, _running_config
    if _TASK is not None:
        _TASK.cancel()
    _TASK = None
    _running_config = None


def status() -> dict:
    running = _TASK is not None and not _TASK.done()
    return {"running": running, "config": _running_config if running else None}
