"""AI 모델 카드 (Model Card) 테스트 — 계획서 1차년도 성과 산출물.

성능 수치는 고정값이 아니라 계획서 기준(Train/Test 80:20 + 시간순 교차검증)으로 실제 계산한 값이어야 한다(요구사항 K03·B04).
"""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.model.demand_forecast import PLAN_MAPE_MAX_PCT, PLAN_R2_MIN, evaluate_plan_targets, mape
from src.model.model_card import build_model_card
from src.pipeline.store import build_city_hourly

client = TestClient(app)


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("admin-test", "admin-pass-1", "admin")
    r = client.post("/auth/login", json={"username": "admin-test", "password": "admin-pass-1"})
    assert r.status_code == 200


@pytest.fixture(scope="module")
def evaluation():
    import numpy as np
    cfg = SimConfig(n_ev=6, weeks=6, n_chargers=10, charger_days=42, n_zones=2, random_seed=7, start_date="2026-11-01")
    rng = np.random.default_rng(7)
    city = build_city_hourly(generate_ev_telemetry(cfg, rng), generate_grid_signal(cfg, rng), cfg)
    return evaluate_plan_targets(city, "ev_available_v2g_kwh")


def test_mape_excludes_near_zero_actuals():
    assert mape([0.0, 10.0, 20.0], [5.0, 11.0, 18.0]) == pytest.approx((0.1 + 0.1) / 2 * 100)
    assert mape([0.0, 0.0], [1.0, 2.0]) is None


def test_plan_evaluation_uses_80_20_split_and_time_series_cv(evaluation):
    ev = evaluation
    assert ev["n_train"] + ev["n_test"] == ev["n_rows"]
    assert abs(ev["n_test"] / ev["n_rows"] - 0.20) < 0.01
    assert ev["data_range"]["train_end"] < ev["data_range"]["test_start"]          # 시간순 — 시험 구간은 학습 뒤
    assert len(ev["cv"]["folds"]) >= 3
    assert all(f["n_train"] < ev["n_train"] for f in ev["cv"]["folds"])           # 교차검증은 학습 구간(80%) 안에서만
    t = ev["test"]
    assert t["mape_pct"] is not None and t["r2"] is not None
    assert ev["targets"]["mape_met"] == (t["mape_pct"] <= PLAN_MAPE_MAX_PCT)
    assert ev["targets"]["r2_met"] == (t["r2"] >= PLAN_R2_MIN)


def test_model_card_numbers_come_from_evaluation_not_constants(evaluation):
    card = build_model_card({"ev_available_v2g_kwh": evaluation}, {"ev_telemetry": "synthetic"})
    for key in ("model_details", "intended_use", "training_data", "evaluation_metrics", "technical_specifications", "caveats_and_limitations"):
        assert key in card
    m = card["evaluation_metrics"]["metrics"]
    assert m["mape_pct"] == evaluation["test"]["mape_pct"] and m["r2_score"] == evaluation["test"]["r2"]
    assert card["training_data"]["total_samples"] == evaluation["n_rows"]
    assert card["technical_specifications"]["hyperparameters"]["n_estimators"] == 800
    assert card["training_data"]["actual_source"].startswith("모의")                 # 합성 데이터는 모의로 표기
    assert "아크릴" in card["model_details"]["developed_by"]["participating_organization"]
    # 예전 고정값(MAPE 8.4·R² 0.892·표본 4,200)이 그대로 남아 있지 않다
    assert not (m["mape_pct"] == 8.4 and m["r2_score"] == 0.892)


def test_model_card_without_evaluation_does_not_invent_numbers():
    card = build_model_card()
    assert card["evaluation_metrics"]["metrics"]["mape_pct"] is None
    assert card["training_data"]["total_samples"] is None


def test_get_model_card_api(admin_env, monkeypatch, evaluation):
    from src.api.routers import models
    monkeypatch.setattr(models, "plan_target_evaluations", lambda: ({"ev_available_v2g_kwh": evaluation}, {"ev_telemetry": "synthetic"}))
    r = client.get("/models/card")
    assert r.status_code == 200
    data = r.json()
    assert data["model_details"]["version"] == "1.0.0"
    assert len(data["training_data"]["dataset_sources"]) >= 5
    assert data["evaluation_metrics"]["target_evaluations"][0]["target_col"] == "ev_available_v2g_kwh"


def test_accuracy_is_one_minus_wape():
    import numpy as np
    """평가의견 11: 예측 정확도 = (1 − WAPE) × 100, 목표 70% 이상."""
    from src.model.demand_forecast import _metrics, PLAN_ACCURACY_MIN_PCT
    m = _metrics(np.array([10.0, 20.0, 30.0]), np.array([12.0, 18.0, 33.0]))
    assert m["wape_pct"] == round(7 / 60 * 100, 2) and m["accuracy_pct"] == round(100 - 7 / 60 * 100, 2)
    assert PLAN_ACCURACY_MIN_PCT == 70.0
