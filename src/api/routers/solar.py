"""태양광 연계 충전소 에뮬레이션 API(관리자) — 나주 태양광 사이트 모의 운영과 하루 발전 곡선."""
from datetime import date as date_type

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.auth.deps import require_admin
from src.pipeline import public_api
from src.solar import pv_model, regional
from src.solar.emulator import MAX_DAYS, PLAN_MODES, EmulationError, run_emulation
from src.solar.site import SiteError, get_site, load_sites

router = APIRouter(prefix="/solar", tags=["solar"], dependencies=[Depends(require_admin)])


class EmulateIn(BaseModel):
    site_id: str = Field("naju_solar_ev", max_length=40)
    start_date: date_type | None = Field(None, description="비우면 오늘(한국시간)")
    days: int = Field(7, ge=1, le=MAX_DAYS)
    seed: int = Field(0, ge=0, le=2**31 - 1)
    plan_with: str = Field("perfect", description=f"{PLAN_MODES} — 최적화가 쓰는 발전량 정보")
    weather_source: str = Field("model", pattern="^(model|kpx_regional)$",
                                description="model=모의 날씨, kpx_regional=전남 실측 발전량으로 보정(인증키·명세·과거 기간 필요)")


def _today_kst() -> date_type:
    return pd.Timestamp.now(tz="Asia/Seoul").date()


@router.get("/sites", summary="에뮬레이션 가능한 태양광 연계 충전소(가정값)")
def sites() -> dict:
    return {"sites": [s.public_dict() for s in load_sites().values()]}


@router.get("/data-sources", summary="공공데이터 연동 현황 — 키·명세 준비 여부(값은 노출하지 않음)")
def data_sources() -> dict:
    spec = public_api.load_spec("kpx_solar_regional")
    return {"sources": [spec.status()],
            "note": "엔드포인트·파라미터는 활용신청 후 받는 명세로 config/public_apis.json을 채운 뒤 spec_verified를 true로 바꾼다."}


@router.post("/emulate", summary="태양광 연계 충전소 모의 운영 — 즉시 충전 vs 태양광 우선 최적 충전")
def emulate(body: EmulateIn) -> dict:
    try:
        site = get_site(body.site_id)
        start = body.start_date or _today_kst()
        sky = None
        if body.weather_source == "kpx_regional":
            sky = regional.sky_factor_for_period(start, body.days, site)
        return run_emulation(site, start, body.days, body.seed, body.plan_with, sky_factor=sky,
                             weather_source=body.weather_source)
    except SiteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except public_api.ApiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (public_api.ApiResponseError, regional.CalibrationError, EmulationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/pv-day", summary="하루 태양광 발전 곡선 — 맑은 날·평균 날씨·시드 날씨")
def pv_day(site_id: str = "naju_solar_ev", day: date_type | None = None, seed: int = Query(0, ge=0)) -> dict:
    try:
        site = get_site(site_id)
    except SiteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    d = pd.Timestamp(day or _today_kst())
    idx = pd.date_range(d, periods=24, freq="h")
    return {
        "site": site.public_dict(), "date": d.date().isoformat(),
        "sky": pv_model.sky_state_for_day(d, seed)[0],
        "clear_pv_kw": [round(v, 2) for v in pv_model.pv_power_kw(idx, site, sky_factor=0.95)],
        "expected_pv_kw": [round(v, 2) for v in pv_model.expected_pv_day(d, site)],
        "seeded_pv_kw": [round(v, 2) for v in pv_model.pv_power_kw(idx, site, seed=seed)],
        "verified": site.verified,
        "note": "평균 날씨(expected)는 예보가 없을 때의 근사, 시드 날씨(seeded)는 에뮬레이션용 모의 날씨다. 모두 가정값.",
    }
