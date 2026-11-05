"""AI 모델 카드 (Model Card) 테스트 — 계획서 1차년도 성과 산출물."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.model.model_card import build_model_card

client = TestClient(app)


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("admin-test", "admin-pass-1", "admin")
    r = client.post("/auth/login", json={"username": "admin-test", "password": "admin-pass-1"})
    assert r.status_code == 200


def test_build_model_card_structure():
    card = build_model_card()
    assert "model_details" in card
    assert "intended_use" in card
    assert "training_data" in card
    assert "evaluation_metrics" in card
    assert "technical_specifications" in card
    assert "caveats_and_limitations" in card

    assert "아크릴" in card["model_details"]["developed_by"]["participating_organization"]
    assert "아우토크립트" in card["model_details"]["developed_by"]["lead_organization"]
    assert card["evaluation_metrics"]["metrics"]["wape_pct"] <= 15.0


def test_get_model_card_api(admin_env):
    r = client.get("/models/card")
    assert r.status_code == 200
    data = r.json()
    assert data["model_details"]["version"] == "1.0.0"
    assert len(data["training_data"]["dataset_sources"]) >= 5
