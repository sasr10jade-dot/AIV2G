"""차량 등록 심사 API(관리자) — 모바일 가입 신청을 PC에서 승인·반려한다.

개인정보 보호: 목록·상세는 번호판을 마스킹한 값만 돌려준다. 전체 번호판은 열람 사유를 받는
`reveal-plate`로만 열리고, 그 호출과 목록 조회·승인·반려는 모두 접속기록(IP 포함)에 남는다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.auth.deps import client_ip, require_admin
from src.privacy import audit, member_history
from src.registry import service

router = APIRouter(prefix="/registrations", tags=["registrations"], dependencies=[Depends(require_admin)])


class ApproveIn(BaseModel):
    ev_id: str | None = Field(None, max_length=40, description="기존 OBD 차량과 연결할 때 지정. 비우면 EV-U0001 형식으로 부여")


class RejectIn(BaseModel):
    reason: str = Field(..., min_length=2, max_length=200)


class RevealIn(BaseModel):
    reason: str = Field(..., min_length=2, max_length=100, description="전체 번호판을 보려는 사유(접속기록에 남음)")


@router.get("", summary="차량 등록 신청 목록(번호판 마스킹)")
def list_registrations(
    request: Request, status: str | None = Query(None), limit: int = Query(200, ge=1, le=500),
    user: dict = Depends(require_admin),
) -> dict:
    try:
        rows = service.list_all(status, limit)
    except service.RegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    pending_count = len(service.list_all("pending", 500))
    audit.log(user["username"], "registrations_list", detail={"status": status, "count": len(rows), "ip": client_ip(request)})
    return {"registrations": [service.view(r) for r in rows], "pending_count": pending_count}


@router.post("/{registration_id}/reveal-plate", summary="전체 번호판 열람 — 사유 필수, 접속기록에 남음")
def reveal_plate(registration_id: str, body: RevealIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    try:
        plate = service.reveal_plate(registration_id, user["username"], body.reason, ip=client_ip(request))
    except service.RegistryError as exc:
        raise HTTPException(status_code=404 if "찾을 수 없" in str(exc) else 422, detail=str(exc)) from exc
    return {"registration_id": registration_id, "plate": plate}


@router.post("/{registration_id}/approve", summary="신청 승인 — 계정에 차량 연결")
def approve(registration_id: str, body: ApproveIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    try:
        reg = service.approve(registration_id, user["username"], body.ev_id)
    except service.RegistryError as exc:
        raise HTTPException(status_code=409 if "이미" in str(exc) else 422, detail=str(exc)) from exc
    audit.log(user["username"], "registration_approve", target=reg["ev_id"],
              detail={"registration_id": registration_id, "ip": client_ip(request)})
    member_history.record(user["username"], reg["username"], "registration_approve",
                          [("status", "pending", "approved"), ("ev_id", None, reg["ev_id"])], ip=client_ip(request))
    return {"status": "approved", "ev_id": reg["ev_id"], "registration": service.view(reg)}


@router.post("/{registration_id}/reject", summary="신청 반려")
def reject(registration_id: str, body: RejectIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    try:
        reg = service.reject(registration_id, user["username"], body.reason)
    except service.RegistryError as exc:
        raise HTTPException(status_code=409 if "찾을 수 없" in str(exc) else 422, detail=str(exc)) from exc
    audit.log(user["username"], "registration_reject", target=registration_id, detail={"ip": client_ip(request)})
    member_history.record(user["username"], reg["username"], "registration_reject",
                          [("status", "pending", "rejected")], ip=client_ip(request), note=body.reason.strip())
    return {"status": "rejected", "registration": service.view(reg)}


@router.get("/member/{username}/history", summary="회원 변경 이력(관리자용 — 처리자·접속 IP 포함, 조회 사실도 기록)")
def member_history_view(username: str, request: Request, limit: int = Query(100, ge=1, le=500),
                        user: dict = Depends(require_admin)) -> dict:
    audit.log(user["username"], "member_history_view", target=username, detail={"ip": client_ip(request)})
    return {"username": username, "history": member_history.timeline(username, limit, for_admin=True)}
