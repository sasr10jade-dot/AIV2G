"""㈜아크릴 예측모델 연계 API — 관리자용(제공 묶음 생성·수신 현황)과 참여기관용(토큰).

참여기관 경로(`/partner/acryl/*`)는 사람 계정이 아니라 기관 간 연계 토큰으로만 연다.
- `ACRYL_API_TOKEN`  : 연계 토큰(필수). 비어 있으면 503 — 설정 전에 실수로 열리지 않게 한다.
- `ACRYL_ALLOWED_IPS`: 허용 접속지(쉼표 구분, 선택). 계획서의 '고정 IP 기반 SSL' 연동 — SSL은 앞단 프록시(HTTPS)에서 종단한다.
요청 헤더 `X-Partner-Token`을 상수 시간 비교로 확인하고, 호출마다 접속기록(audit)에 남긴다.
"""
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src.acryl import link
from src.auth.deps import client_ip, require_admin
from src.datagen.config import SimConfig
from src.pipeline.source import load_scenario
from src.pipeline.store import build_city_hourly
from src.privacy import audit

router = APIRouter(tags=["acryl"])


def require_partner(request: Request) -> str:
    token = os.environ.get("ACRYL_API_TOKEN", "")
    if not token:
        raise HTTPException(status_code=503, detail="참여기관 연계가 아직 설정되지 않았습니다")
    allowed = [ip.strip() for ip in os.environ.get("ACRYL_ALLOWED_IPS", "").split(",") if ip.strip()]
    ip = client_ip(request)
    if allowed and ip not in allowed:
        audit.log("partner:acryl", "partner_denied", target=request.url.path, detail={"ip": ip, "reason": "ip"})
        raise HTTPException(status_code=403, detail="허용되지 않은 접속지입니다")
    given = request.headers.get("x-partner-token", "")
    if not hmac.compare_digest(given.encode(), token.encode()):
        audit.log("partner:acryl", "partner_denied", target=request.url.path, detail={"ip": ip, "reason": "token"})
        raise HTTPException(status_code=401, detail="연계 토큰이 올바르지 않습니다")
    return "partner:acryl"


class ExportIn(BaseModel):
    period_start: str | None = Field(None, description="제공 기간 시작일(YYYY-MM-DD, 비우면 처음부터)")
    period_end: str | None = Field(None, description="제공 기간 종료일(YYYY-MM-DD, 비우면 끝까지)")


class ForecastPoint(BaseModel):
    timestamp: str
    value: float
    lower: float | None = None
    upper: float | None = None


class ForecastBatchIn(BaseModel):
    batch_id: str = Field(..., max_length=64)
    model_id: str = Field(..., max_length=64)
    model_version: str = Field(..., max_length=64)
    target: str = Field(..., description=f"예측 대상: {', '.join(link.FORECAST_TARGETS)}")
    scope: str = Field("city", max_length=64, description="예측 범위(도시 전체=city)")
    issued_at: str = Field(..., description="예측 발행 시각(ISO8601)")
    points: list[ForecastPoint] = Field(..., max_length=link.MAX_POINTS_PER_BATCH)


def _err(exc: link.LinkError, code: int = 422):
    raise HTTPException(status_code=code, detail=str(exc)) from exc


# ------------------------------------------------------------------ 관리자 --

@router.post("/acryl/exports", summary="학습용 정제 데이터 제공 묶음 생성(가명처리·SHA-256 manifest)")
def create_export(body: ExportIn, user: dict = Depends(require_admin)) -> dict:
    try:
        manifest = link.export_training_dataset(user["username"], body.period_start, body.period_end)
    except link.LinkError as exc:
        _err(exc)
    audit.log(user["username"], "acryl_export_create", target=manifest["export_id"],
              detail={"period": manifest["period"], "rows": {f["name"]: f["rows"] for f in manifest["files"]}})
    return manifest


@router.get("/acryl/exports", summary="학습 데이터 제공 이력", dependencies=[Depends(require_admin)])
def list_exports_admin() -> dict:
    return {"exports": link.list_exports()}


@router.get("/acryl/forecasts/status", summary="예측 결과 수신 현황(대상별 마지막 배치·모델 버전)",
            dependencies=[Depends(require_admin)])
def forecast_status() -> dict:
    return {"targets": link.forecast_status()}


@router.get("/acryl/forecasts/{target}/evaluation", summary="수신 예측 vs 실측 오차(MAE·예측률)",
            dependencies=[Depends(require_admin)])
def forecast_evaluation(target: str) -> dict:
    if target not in link.FORECAST_TARGETS:
        raise HTTPException(status_code=404, detail="알 수 없는 예측 대상입니다")
    cfg = SimConfig()
    ev_df, _c, grid_df, is_real = load_scenario(cfg)
    result = link.evaluate_against_actual(target, build_city_hourly(ev_df, grid_df, cfg))
    result["actual_is_real"] = is_real
    return result


# ------------------------------------------------------------------ 참여기관(토큰) --

@router.get("/partner/acryl/exports", summary="[참여기관] 제공 묶음 목록")
def partner_list_exports(actor: str = Depends(require_partner)) -> dict:
    return {"exports": link.list_exports()}


@router.get("/partner/acryl/exports/{export_id}/{name}", summary="[참여기관] 제공 묶음 파일 내려받기(manifest.json·CSV)")
def partner_download(export_id: str, name: str, request: Request, actor: str = Depends(require_partner)):
    try:
        path = link.export_file(export_id, name)
    except link.LinkError as exc:
        _err(exc, 404)
    audit.log(actor, "acryl_export_download", target=f"{export_id}/{name}", detail={"ip": client_ip(request)})
    return FileResponse(path, filename=name)


@router.post("/partner/acryl/forecasts", summary="[참여기관] 예측 추론 결과 배치 수신(일 배치)")
def partner_receive_forecasts(body: ForecastBatchIn, request: Request, actor: str = Depends(require_partner)) -> dict:
    try:
        result = link.receive_forecast_batch(body.model_dump())
    except link.LinkError as exc:
        _err(exc)
    audit.log(actor, "acryl_forecast_receive", target=body.batch_id,
              detail={"target": body.target, "model_version": body.model_version, "points": result["points"],
                      "duplicate": result["duplicate"], "ip": client_ip(request)})
    return result
