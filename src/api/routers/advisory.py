"""차주 추천 안내 API — 출발시각·현재 SOC·필요 SOC를 입력받아 열화·효율을 반영한 충·방전 계획을
만들고 발행 기록을 남긴다. 차주가 따랐는지(응답)도 기록해 수용률을 추적한다."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.advisory import ai_ops_kpi, service
from src.auth.deps import require_admin
from src.datagen.config import SimConfig
from src.grid.tariff import TariffError
from src.model.plugin_pattern import InsufficientHistoryError, UnknownVehicleError
from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import plan_next_session
from src.pipeline.source import load_scenario
from src.privacy import audit

router = APIRouter(prefix="/advisory", tags=["advisory"])


class IssueIn(BaseModel):
    ev_id: str
    departure_time: str | None = Field(None, description="ISO 시각. 없으면 이력 기반 예측")
    current_soc_pct: float | None = Field(None, ge=0, le=100)
    need_soc_pct: float | None = Field(None, ge=20, le=100)
    tariff_id: str | None = Field(None, description="충전 요금표 ID(config/tariffs.json). 주면 차주 가격 모델로 계획")
    compensation_id: str | None = Field(None, description="방전 보상 시나리오 ID(config/compensation.json)")
    channel: str = "api"


class RespondIn(BaseModel):
    status: str


@router.post("/issue", summary="추천 계획 생성·발행")
def issue(body: IssueIn, user: dict = Depends(require_admin)) -> dict:
    cfg = SimConfig()
    ev_df, _c, grid_df, _real = load_scenario(cfg)
    try:
        plan = plan_next_session(
            ev_df, grid_df, cfg, body.ev_id, economics=EconomicsParams.load(),
            departure_time=body.departure_time, current_soc_pct=body.current_soc_pct, need_soc_pct=body.need_soc_pct,
            tariff_id=body.tariff_id, compensation_id=body.compensation_id,
        )
        advisory_id = service.issue(body.ev_id, plan, body.channel, inputs=body.model_dump(exclude={"ev_id", "channel"}))
    except UnknownVehicleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (InsufficientHistoryError, service.AdvisoryError, TariffError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.log(user["username"], "advisory_issue", target=body.ev_id, detail={"advisory_id": advisory_id})
    return {"advisory_id": advisory_id, "plan": plan}


@router.get("/ai-ops-kpi", summary="AI 운영 지표 — 안전 검증 통과율·방전 참여 철회율·EV Bot 호출(감사로그·동의 이력 집계)")
def ai_ops_kpi_view(user: dict = Depends(require_admin)) -> dict:
    return ai_ops_kpi.compute()


@router.post("/{advisory_id}/respond", summary="차주 응답 기록(accepted/declined/expired)")
def respond(advisory_id: str, body: RespondIn, user: dict = Depends(require_admin)) -> dict:
    try:
        service.respond(advisory_id, body.status)
    except service.AdvisoryError as exc:
        raise HTTPException(status_code=409 if "이미" in str(exc) else 422, detail=str(exc)) from exc
    audit.log(user["username"], "advisory_respond", target=advisory_id, detail={"status": body.status})
    return {"status": "ok"}


@router.get("", summary="추천 목록과 수용률", dependencies=[Depends(require_admin)])
def list_all(ev_id: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict:
    return {"advisories": service.list_advisories(ev_id, limit), "stats": service.acceptance_stats()}
