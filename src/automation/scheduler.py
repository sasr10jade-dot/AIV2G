"""WS2 자동운영 고도화 — 주간 자동 재학습 사이클.

기존 "예측 → 운영자 판단 → 실증" 구조를, 사람 개입 없이 매 주기 아래 4단계로 도는
"예측 → 최적화 → 운영 → 검증" 자동화 구조로 전환한다.

  1) 활성 모델을 신규 주차 데이터에 그대로 적용해 드리프트 측정
  2) 드리프트가 임계치(기본 20%)를 넘으면 신규 데이터로 후보 모델 재학습
  3) 후보가 "재학습 없이 방치했을 때(stale)"보다 뚜렷이 낫지 않으면 승격을 거부하고
     기존 활성 버전을 유지 — 잘못된 재학습이 그대로 배포되는 것을 막는 자동 안전장치
  4) 통과한 후보만 레지스트리에 새 버전으로 등록·승격
"""
from dataclasses import replace

from sklearn.metrics import mean_absolute_error

from src.datagen.config import SimConfig
from src.datagen.scenario import synthetic_city_hourly
from src.model.demand_forecast import train_forecast_model, TEST_HOURS
from src.model.features import build_features
from . import issues
from .model_registry import ModelRegistry

DRIFT_TRIGGER_PCT = 20.0
MIN_IMPROVEMENT_TO_PROMOTE_PCT = 2.0  # stale 대비 이 이상 개선돼야 승격


def bootstrap(registry: ModelRegistry, base_cfg: SimConfig, target_col: str) -> None:
    """레지스트리가 비어 있으면 최초 모델(v1)을 등록해 사이클을 시작할 수 있게 한다."""
    if registry.active_version() is not None:
        return
    city_hourly = synthetic_city_hourly(base_cfg)   # 서포터즈 OBD + 거점 충전 세션(결합 데이터셋)
    result = train_forecast_model(city_hourly, target_col)
    registry.register(result.model, result.feature_cols, base_cfg.weeks, result.mae, result.rmse, result.n_train,
                      data_range=result.data_range, data_source="synthetic", holdout_mape=result.mape, holdout_r2=result.r2)


def run_cycle(
    registry: ModelRegistry, base_cfg: SimConfig, week_offset: int, target_col: str, fleet_growth: int = 0
) -> dict:
    cfg_new = replace(base_cfg, weeks=base_cfg.weeks + week_offset, n_ev=base_cfg.n_ev + fleet_growth)
    city_hourly = synthetic_city_hourly(cfg_new)
    feat_df, _ = build_features(city_hourly, target_col)
    new_period = feat_df.iloc[-TEST_HOURS:]

    active_model, active_features, active_info = registry.load()
    stale_pred = active_model.predict(new_period[active_features]).clip(min=0)
    stale_mae = float(mean_absolute_error(new_period[target_col], stale_pred))
    drift_pct = (stale_mae - active_info["holdout_mae"]) / active_info["holdout_mae"] * 100

    cycle = {
        "week": cfg_new.weeks,
        "n_ev": cfg_new.n_ev,
        "active_version_before": active_info["version"],
        "stale_mae": round(stale_mae, 3),
        "drift_pct": round(drift_pct, 1),
        "retrain_triggered": drift_pct > DRIFT_TRIGGER_PCT,
        "active_version_after": active_info["version"],
        "action": "NO_RETRAIN",
        "candidate_mae": None,
    }
    if not cycle["retrain_triggered"]:
        return cycle
    issues.record("retrain", f"예측 오차 증가(드리프트 {DRIFT_TRIGGER_PCT:.0f}% 초과)로 재학습 실행", kind="data",
                  detail={"target": target_col, "week": cfg_new.weeks, "drift_pct": cycle["drift_pct"]})

    candidate = train_forecast_model(city_hourly, target_col)
    cycle["candidate_mae"] = candidate.mae
    improvement_pct = (stale_mae - candidate.mae) / stale_mae * 100 if stale_mae else 0.0

    if improvement_pct >= MIN_IMPROVEMENT_TO_PROMOTE_PCT:
        version = registry.register(
            candidate.model, candidate.feature_cols, cfg_new.weeks, candidate.mae, candidate.rmse, candidate.n_train,
            data_range=candidate.data_range, data_source="synthetic",
            holdout_mape=candidate.mape, holdout_r2=candidate.r2,
        )
        cycle["action"] = "PROMOTED"
        cycle["active_version_after"] = version
    else:
        cycle["action"] = "REJECTED_KEEP_ACTIVE"
        issues.record("retrain", "재학습 후보가 기존 모델보다 충분히 낫지 않아 승격 거부(기존 모델 유지)", kind="exception",
                      detail={"target": target_col, "week": cfg_new.weeks, "stale_mae": cycle["stale_mae"],
                              "candidate_mae": candidate.mae, "improvement_pct": round(improvement_pct, 1)})

    return cycle


def run_cycle_on_data(registry: ModelRegistry, city_hourly, target_col: str, data_source: str) -> dict:
    """운영 데이터(실측이 있으면 실측)로 한 주기를 돈다 — 요구사항 Y2B01·Y2B02.

    합성 데이터를 새로 만드는 `run_cycle`(데모)과 달리, 지금 쌓인 데이터의 마지막 2주에 활성 모델을 적용해
    드리프트를 재고, 임계치를 넘으면 같은 데이터로 후보를 재학습해 승격/거부한다. 레지스트리가 비어 있으면 첫 모델을 등록한다.
    결과(재학습 전후 오차 포함)를 돌려주고, 예외 상황은 modeling_issues에 남긴다.
    """
    if registry.active_version() is None:
        first = train_forecast_model(city_hourly, target_col)
        v = registry.register(first.model, first.feature_cols, None, first.mae, first.rmse, first.n_train,
                              data_range=first.data_range, data_source=data_source, holdout_mape=first.mape, holdout_r2=first.r2)
        return {"target_col": target_col, "action": "BOOTSTRAPPED", "active_version_after": v, "candidate_mae": first.mae,
                "data_source": data_source}
    feat_df, _ = build_features(city_hourly, target_col)
    new_period = feat_df.iloc[-TEST_HOURS:]
    active_model, active_features, active_info = registry.load()
    missing = [c for c in active_features if c not in new_period.columns]
    if missing:
        issues.record("retrain", "활성 모델 피처가 현재 데이터에 없어 드리프트를 잴 수 없음", kind="exception",
                      detail={"target": target_col, "missing": missing[:10]})
        return {"target_col": target_col, "action": "SKIPPED_FEATURE_MISMATCH", "missing": missing[:10]}
    stale_pred = active_model.predict(new_period[active_features]).clip(min=0)
    stale_mae = float(mean_absolute_error(new_period[target_col], stale_pred))
    base = active_info["holdout_mae"] or 1e-9
    drift_pct = (stale_mae - base) / base * 100
    cycle = {"target_col": target_col, "data_source": data_source, "active_version_before": active_info["version"],
             "stale_mae": round(stale_mae, 3), "drift_pct": round(drift_pct, 1),
             "retrain_triggered": drift_pct > DRIFT_TRIGGER_PCT, "active_version_after": active_info["version"],
             "action": "NO_RETRAIN", "candidate_mae": None}
    if not cycle["retrain_triggered"]:
        return cycle
    candidate = train_forecast_model(city_hourly, target_col)
    cycle["candidate_mae"] = candidate.mae
    improvement_pct = (stale_mae - candidate.mae) / stale_mae * 100 if stale_mae else 0.0
    cycle["improvement_pct"] = round(improvement_pct, 1)
    if improvement_pct >= MIN_IMPROVEMENT_TO_PROMOTE_PCT:
        cycle["active_version_after"] = registry.register(
            candidate.model, candidate.feature_cols, None, candidate.mae, candidate.rmse, candidate.n_train,
            data_range=candidate.data_range, data_source=data_source, holdout_mape=candidate.mape, holdout_r2=candidate.r2)
        cycle["action"] = "PROMOTED"
    else:
        cycle["action"] = "REJECTED_KEEP_ACTIVE"
        issues.record("retrain", "재학습 후보가 기존 모델보다 충분히 낫지 않아 승격 거부(기존 모델 유지)", kind="exception",
                      detail={"target": target_col, "stale_mae": cycle["stale_mae"], "candidate_mae": candidate.mae,
                              "improvement_pct": cycle["improvement_pct"]})
    return cycle
