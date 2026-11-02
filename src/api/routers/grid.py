"""한전 계통 수요반응(DR) 이벤트 관리 API — CEMS의 "외부 계통 신호 수신" 계층.

실제 한전 DR 신호를 받는 공개 API가 없어(이전 분석 결과) 관리자가 수동으로 DR
이벤트를 등록하는 스텁이다. 등록된 이벤트는 `src/optimizer/poc_compare.py`의
가격 신호에 자동 반영되어, 그 시간대에 V2G 방전이 더 강하게 유인된다.
"""
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.auth.deps import require_admin
from src.grid import dr_emulator, dr_events

router = APIRouter(prefix="/grid", tags=["grid"], dependencies=[Depends(require_admin)])


class CreateDrEventRequest(BaseModel):
    start_ts: str = Field(..., description="DR 이벤트 시작 시각 (ISO8601)")
    end_ts: str = Field(..., description="DR 이벤트 종료 시각 (ISO8601)")
    severity: str = Field("medium", description="'low' | 'medium' | 'high' — 높을수록 방전 유인 가격 프리미엄이 커짐")
    reason: str = Field("", description="이벤트 사유(선택) — 예: '동계 피크 예상'")

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: str) -> str:
        if v not in dr_events.SEVERITY_MULTIPLIER:
            raise ValueError(f"severity는 {list(dr_events.SEVERITY_MULTIPLIER)} 중 하나여야 합니다")
        return v


@router.get("/dr-events", summary="등록된 DR 이벤트 목록")
def get_dr_events() -> list:
    return dr_events.list_dr_events()


@router.post("/dr-events", summary="DR 이벤트 등록 (한전 DR 신호 수신 시뮬레이션)")
def create_dr_event(req: CreateDrEventRequest) -> dict:
    try:
        start_ts = pd.Timestamp(req.start_ts)
        end_ts = pd.Timestamp(req.end_ts)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"시각 형식이 올바르지 않습니다: {exc}") from exc

    try:
        event_id = dr_events.create_dr_event(start_ts, end_ts, severity=req.severity, reason=req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "ok", "event_id": event_id}


@router.delete("/dr-events/{event_id}", summary="DR 이벤트 취소")
def remove_dr_event(event_id: str) -> dict:
    try:
        dr_events.delete_dr_event(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@router.delete("/dr-events", summary="등록된 DR 이벤트 전체 삭제")
def clear_all_dr_events() -> dict:
    dr_events.clear_dr_events()
    return {"status": "ok"}


@router.post("/dr-emulator/start", summary="DR 신호 자동 발생 시작 (데모/개발용 — 실제 한전 연동 아님)")
async def start_dr_emulator(
    interval_sec: float = 60.0, trigger_prob: float = 0.3, min_hours: float = 1.0, max_hours: float = 4.0
) -> dict:
    """`interval_sec`마다 `trigger_prob` 확률로 랜덤한 심각도·지속시간의 DR 이벤트를
    자동 등록한다 — 관리자가 매번 수동으로 `/grid/dr-events`를 호출하지 않아도 데모
    중 DR 반응을 계속 보여줄 수 있다. **실제 한전 신호 인터페이스를 흉내낸 것이
    아니라 이미 있는 등록 API를 대신 호출해주는 타이머**일 뿐이라는 점에 유의 —
    실제 한전 연동이 확정되면 교체 대상이다.

    `async def`여야 한다 — `dr_emulator.start_emulator()`가 내부에서
    `asyncio.create_task()`를 호출하는데, FastAPI는 동기(`def`) 엔드포인트를 별도
    워커 스레드에서 돌려 그 안엔 실행 중인 이벤트 루프가 없다("no running event
    loop" 오류로 실제로 재현됨) — OCPP 에뮬레이터의 `start_emulator`도 같은 이유로
    `async def`다."""
    if not (0.0 < trigger_prob <= 1.0):
        raise HTTPException(status_code=422, detail="trigger_prob은 0보다 크고 1 이하여야 합니다")
    if interval_sec < 5.0:
        raise HTTPException(status_code=422, detail="interval_sec은 5 이상이어야 합니다(과도한 폴링 방지)")
    dr_emulator.start_emulator(interval_sec=interval_sec, trigger_prob=trigger_prob, min_hours=min_hours, max_hours=max_hours)
    return {"status": "ok", **dr_emulator.status()}


@router.post("/dr-emulator/stop", summary="DR 신호 자동 발생 중지")
def stop_dr_emulator() -> dict:
    dr_emulator.stop_emulator()
    return {"status": "ok"}


@router.get("/dr-emulator/status", summary="DR 에뮬레이터 실행 상태")
def get_dr_emulator_status() -> dict:
    return dr_emulator.status()
