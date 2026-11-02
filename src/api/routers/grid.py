"""한전 계통 수요반응(DR) 이벤트 관리 API — CEMS의 "외부 계통 신호 수신" 계층.

실제 한전 DR 신호를 받는 공개 API가 없어(이전 분석 결과) 관리자가 수동으로 DR
이벤트를 등록하는 스텁이다. 등록된 이벤트는 `src/optimizer/poc_compare.py`의
가격 신호에 자동 반영되어, 그 시간대에 V2G 방전이 더 강하게 유인된다.
"""
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.auth.deps import require_admin
from src.grid import dr_events

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

