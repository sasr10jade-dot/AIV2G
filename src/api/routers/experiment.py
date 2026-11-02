"""실험·인센티브 API(최고관리자 전용) — 그룹 배정, 배정 조회, 인센티브 정산 미리보기. 변경은 감사로그에 남긴다."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.deps import require_super_admin
from src.events import discharge
from src.experiment import assign as assign_mod
from src.experiment.incentive import IncentivePolicy, compute_incentive
from src.pipeline import store
from src.privacy import audit, consent

router = APIRouter(prefix="/experiment", tags=["experiment"])


class AssignIn(BaseModel):
    experiment_id: str
    ev_models: dict[str, str] = Field(..., description="{ev_id: 차종}")
    treatment_ratio: float = 0.5


@router.post("/assign", summary="실험군/대조군 배정(기존 배정은 유지)")
def assign(body: AssignIn, user: dict = Depends(require_super_admin)) -> dict:
    try:
        groups = assign_mod.assign(body.experiment_id, body.ev_models, body.treatment_ratio)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.log(user["username"], "experiment_assign", target=body.experiment_id, detail={"n": len(groups)})
    return {"experiment_id": body.experiment_id, "groups": groups}


@router.get("/incentive", summary="인센티브 정산 미리보기(동의한 차량만, TEST 제외)")
def incentive_preview(user: dict = Depends(require_super_admin)) -> dict:
    policy = IncentivePolicy.load()
    con = store.get_connection()
    try:
        events = discharge.list_events(limit=100000, con=con)
    finally:
        con.close()
    eligible = {e["ev_id"] for e in events if consent.has_consent(e["ev_id"], "incentive")}
    audit.log(user["username"], "incentive_preview")
    return compute_incentive(events, policy, eligible_evs=eligible)
