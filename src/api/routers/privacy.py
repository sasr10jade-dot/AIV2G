"""개인정보 보호 운영 API — 동의 등록·철회·조회, 감사로그 조회·무결성 검증(최고관리자 전용).
모든 변경은 감사로그에 남긴다."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.deps import require_super_admin
from src.privacy import audit, consent

router = APIRouter(prefix="/privacy", tags=["privacy"])


class ConsentIn(BaseModel):
    ev_id: str
    purpose: str
    version: str


@router.get("/consents", summary="동의 이력 조회")
def list_consents(ev_id: str | None = None, user: dict = Depends(require_super_admin)) -> dict:
    audit.log(user["username"], "consent_view", target=ev_id)
    return {"consents": consent.list_consents(ev_id)}


@router.post("/consents/grant", summary="동의 등록")
def grant(body: ConsentIn, user: dict = Depends(require_super_admin)) -> dict:
    try:
        consent.grant(body.ev_id, body.purpose, body.version, recorded_by=user["username"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.log(user["username"], "consent_grant", target=body.ev_id,
              detail={"purpose": body.purpose, "version": body.version})
    return {"status": "ok"}


@router.post("/consents/withdraw", summary="동의 철회")
def withdraw(body: ConsentIn, user: dict = Depends(require_super_admin)) -> dict:
    try:
        changed = consent.withdraw(body.ev_id, body.purpose)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.log(user["username"], "consent_withdraw", target=body.ev_id,
              detail={"purpose": body.purpose, "changed": changed})
    return {"status": "ok", "changed": changed}


@router.get("/audit", summary="최근 감사로그와 체인 무결성 검증")
def audit_view(limit: int = 50, user: dict = Depends(require_super_admin)) -> dict:
    return {"chain": audit.verify_chain(), "recent": audit.recent(min(max(limit, 1), 500))}
