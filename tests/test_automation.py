import numpy as np

from src.automation.model_registry import ModelRegistry
from src.automation.scheduler import bootstrap, run_cycle

TARGET_COL = "ev_charging_demand_kw"


def test_bootstrap_registers_first_active_version(tmp_path, medium_cfg):
    registry = ModelRegistry(TARGET_COL, base_dir=tmp_path)
    assert registry.active_version() is None

    bootstrap(registry, medium_cfg, TARGET_COL)

    assert registry.active_version() == 1
    _model, _features, info = registry.load()
    assert info["version"] == 1
    assert np.isfinite(info["holdout_mae"])
    # 계획서 6장 — 같은 조건으로 재현할 수 있게 학습·검증 구간과 데이터 출처를 남긴다
    rng = info["data_range"]
    assert rng["train_start"] < rng["train_end"] < rng["test_start"] <= rng["test_end"]
    assert info["data_source"] == "synthetic" and info["feature_count"] == len(_features) and info["registered_at"]


def test_bootstrap_is_idempotent(tmp_path, medium_cfg):
    registry = ModelRegistry(TARGET_COL, base_dir=tmp_path)
    bootstrap(registry, medium_cfg, TARGET_COL)
    bootstrap(registry, medium_cfg, TARGET_COL)

    assert registry.active_version() == 1


def test_run_cycle_no_retrain_without_drift(tmp_path, medium_cfg):
    registry = ModelRegistry(TARGET_COL, base_dir=tmp_path)
    bootstrap(registry, medium_cfg, TARGET_COL)

    cycle = run_cycle(registry, medium_cfg, week_offset=0, target_col=TARGET_COL)

    assert cycle["action"] == "NO_RETRAIN"
    assert cycle["retrain_triggered"] is False
    assert registry.active_version() == cycle["active_version_after"] == 1


def test_run_cycle_promotes_or_rejects_when_drift_triggered(tmp_path, medium_cfg):
    """드리프트가 감지되면 재학습 후보를 만들고, 개선폭 기준을 충족할 때만 승격한다."""
    registry = ModelRegistry(TARGET_COL, base_dir=tmp_path)
    bootstrap(registry, medium_cfg, TARGET_COL)

    cycle = run_cycle(registry, medium_cfg, week_offset=1, target_col=TARGET_COL, fleet_growth=2)

    assert cycle["action"] in ("PROMOTED", "REJECTED_KEEP_ACTIVE", "NO_RETRAIN")
    if cycle["retrain_triggered"]:
        assert cycle["candidate_mae"] is not None
        if cycle["action"] == "PROMOTED":
            assert registry.active_version() == 2
            assert cycle["active_version_after"] == 2
        else:
            assert cycle["action"] == "REJECTED_KEEP_ACTIVE"
            assert registry.active_version() == 1
        # 계획서 4.1 다 — 드리프트·승격 거부는 모델링 예외 기록에 남는다
        from src.automation import issues
        stages = [(x["stage"], x["kind"]) for x in issues.list_issues()]
        assert ("retrain", "data") in stages
        assert (("retrain", "exception") in stages) == (cycle["action"] == "REJECTED_KEEP_ACTIVE")
