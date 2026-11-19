"""모델 레지스트리 및 모델 카드 조회 API (Monitoring/Reporting 레이어 입력)."""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException

from src.auth.deps import require_admin
from src.automation.model_registry import ModelRegistry
from src.model.model_card import build_model_card

router = APIRouter(prefix="/models", tags=["models"])

KNOWN_TARGETS = ["ev_charging_demand_kw", "ev_available_v2g_kwh"]

_EVAL_TTL_SEC = 600
_eval_cache: dict = {}
_eval_lock = threading.Lock()


def plan_target_evaluations() -> tuple[dict, dict]:
    """지금 데이터로 계획서 기준(80:20 + 교차검증) 성능을 실제 계산한다. 데이터가 바뀌거나 10분이 지나면 다시 계산."""
    from src.datagen.config import SimConfig
    from src.model.demand_forecast import evaluate_plan_targets
    from src.pipeline import source
    from src.pipeline.store import build_city_hourly

    key = source._raw_fingerprint()
    with _eval_lock:
        hit = _eval_cache.get("v")
        if hit and hit[0] == key and time.time() - hit[1] < _EVAL_TTL_SEC:
            return hit[2], hit[3]
        cfg = SimConfig()
        ev_df, _ch, grid_df, _real = source.load_scenario(cfg)
        city_hourly = build_city_hourly(ev_df, grid_df, cfg)
        evaluations = {tc: evaluate_plan_targets(city_hourly, tc) for tc in KNOWN_TARGETS}
        prov = source.provenance()
        _eval_cache["v"] = (key, time.time(), evaluations, prov)
        return evaluations, prov


@router.get("/card", summary="AI 기반 최적 V2G 예측모델 모델 카드 (공식 산출물)", dependencies=[Depends(require_admin)])
def get_model_card() -> dict:
    """사업수행계획서 1차년도 성과지표 산출물인 AI 모델 설명 자료(Model Card)를 반환합니다.
    성능(MAPE·R²·WAPE·RMSE)은 계획서 기준(Train/Test 80:20 + 시간순 교차검증)으로 지금 데이터에서 실제 계산한 값입니다."""
    evaluations, prov = plan_target_evaluations()
    return build_model_card(evaluations, prov)


@router.get("/evaluation", summary="계획서 성능 목표(MAPE ≤20%, R² ≥0.70) 평가 결과 — 폴드별 상세", dependencies=[Depends(require_admin)])
def get_plan_evaluation() -> dict:
    evaluations, prov = plan_target_evaluations()
    return {"provenance": prov, "evaluations": evaluations}


@router.get("", summary="등록된 예측 타깃 전체 목록·활성 버전", dependencies=[Depends(require_admin)])
def list_models() -> dict:
    """등록된 예측 타깃별 활성 모델 버전 현황."""
    return {target_col: {"active_version": ModelRegistry(target_col).active_version()} for target_col in KNOWN_TARGETS}


@router.get("/{target_col}", summary="특정 예측 타깃의 모델 상세 정보", dependencies=[Depends(require_admin)])
def get_model_info(target_col: str) -> dict:
    """특정 예측 타깃(target_col)의 활성 모델 버전·피처 목록·학습/홀드아웃 성능을 반환합니다.

    target_col은 `ev_charging_demand_kw`(EV 충전수요) 또는 `ev_available_v2g_kwh`
    (EV 가용 V2G 자원) 중 하나다. 등록된 모델이 없으면 404를 반환한다.
    """
    registry = ModelRegistry(target_col)
    if registry.active_version() is None:
        raise HTTPException(status_code=404, detail=f"등록된 모델이 없습니다: {target_col}")
    _model, feature_cols, info = registry.load()
    return {"target_col": target_col, "feature_cols": feature_cols, **info}
