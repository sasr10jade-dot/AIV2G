"""V2G 최적 충·방전 의사결정 API (WS3/WS4)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.deps import require_admin
from src.datagen.config import SimConfig
from src.grid.pricing import DEFAULT_COMPENSATION_ID, DEFAULT_TARIFF_ID, load_compensations, price_model_for_grid
from src.grid.tariff import TariffError, load_tariffs
from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import (
    compare_baseline_vs_optimized,
    compute_fleet_revenue_comparison,
    compute_owner_cost_comparison,
)
from src.pipeline.source import load_scenario

router = APIRouter(prefix="/optimize", tags=["optimize"], dependencies=[Depends(require_admin)])


class PocCompareRequest(BaseModel):
    max_charge_kw: float = Field(7.0, gt=0, le=50, description="세션당 최대 충전 출력(kW)")
    max_discharge_kw: float = Field(7.0, ge=0, le=50, description="세션당 최대 V2G 방전 출력(kW)")
    reserve_soc: float = Field(0.20, ge=0, le=0.5, description="이 SOC 이하로는 방전하지 않는 예비 배터리 비율")
    target_soc: float = Field(0.90, ge=0.5, le=0.99, description="세션 종료까지 채우려는 목표 SOC 비율")
    tariff_id: str | None = Field(None, description="충전 요금표 ID. tariff_id·compensation_id 중 하나라도 주면 차주 관점 비용(owner_view)을 함께 계산")
    compensation_id: str | None = Field(None, description="방전 보상 시나리오 ID")


@router.get("/price-options", summary="선택 가능한 충전 요금표·방전 보상 시나리오")
def price_options() -> dict:
    return {
        "tariffs": [{"id": t.tariff_id, "label": t.label, "verified": t.verified} for t in load_tariffs().values()],
        "compensations": [
            {"id": c.compensation_id, "label": c.label, "kind": c.kind, "verified": c.verified}
            for c in load_compensations().values()
        ],
    }


@router.post("/poc-compare", summary="일반충전 vs AI-V2G 최적화 피크저감 비교")
def poc_compare(req: PocCompareRequest) -> dict:
    """현재 시나리오(합성 또는 업로드된 실증 데이터)에 대해 일반충전(baseline)과
    AI-V2G 최적화(LP 기반 valley-filling) 적용 시의 집계 부하곡선을 비교하고,
    피크 저감률을 계산한다. 요금표·보상 시나리오를 주면 차주 관점 비용(owner_view)도 계산한다."""
    cfg = SimConfig()
    ev_df, _charger_df, grid_df, is_real = load_scenario(cfg)

    price_model = None
    if req.tariff_id is not None or req.compensation_id is not None:
        try:
            price_model = price_model_for_grid(
                grid_df, req.tariff_id or DEFAULT_TARIFF_ID, req.compensation_id or DEFAULT_COMPENSATION_ID,
            )
        except TariffError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    comparison = compare_baseline_vs_optimized(
        ev_df, grid_df, cfg,
        max_charge_kw=req.max_charge_kw, max_discharge_kw=req.max_discharge_kw,
        reserve_soc=req.reserve_soc, target_soc=req.target_soc, price_model=price_model,
    )
    baseline_peak = float(comparison["baseline_kw"].max())
    optimized_peak = float(comparison["optimized_kw"].max())
    peak_reduction_pct = round((baseline_peak - optimized_peak) / baseline_peak * 100, 1) if baseline_peak else 0.0
    revenue = compute_fleet_revenue_comparison(comparison, grid_df)

    result = {
        "is_real_data": is_real,
        "baseline_peak_kw": round(baseline_peak, 2),
        "optimized_peak_kw": round(optimized_peak, 2),
        "peak_reduction_pct": peak_reduction_pct,
        "rows": len(comparison),
        **revenue,
    }
    if price_model is not None:
        result["owner_view"] = compute_owner_cost_comparison(comparison, price_model, EconomicsParams.load())
    return result
