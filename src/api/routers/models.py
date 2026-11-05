"""모델 레지스트리 및 모델 카드 조회 API (Monitoring/Reporting 레이어 입력)."""
from fastapi import APIRouter, Depends, HTTPException

from src.auth.deps import require_admin
from src.automation.model_registry import ModelRegistry
from src.model.model_card import build_model_card

router = APIRouter(prefix="/models", tags=["models"])

KNOWN_TARGETS = ["ev_charging_demand_kw", "ev_available_v2g_kwh"]


@router.get("/card", summary="AI 기반 최적 V2G 예측모델 모델 카드 (공식 산출물)")
def get_model_card() -> dict:
    """사업수행계획서 1차년도 성과지표 산출물인 AI 모델 설명 자료(Model Card)를 반환합니다."""
    return build_model_card()


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
