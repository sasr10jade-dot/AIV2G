"""AI 모델 카드 (Model Card) 테스트 — 계획서 1차년도 성과 산출물."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.model.model_card import build_model_card

client = TestClient(app)


def test_build_model_card_structure():
    card = build_model_card()
    assert "model_details" in card
    assert "intended_use" in card
    assert "training_data" in card
    assert "evaluation_metrics" in card
    assert "technical_specifications" in card
    assert "caveats_and_limitations" in card

    # 주요 필수 내용 확인
    assert "아크릴" in card["model_details"]["developed_by"]["participating_organization"]
    assert "아우토크립트" in card["model_details"]["developed_by"]["lead_organization"]
    assert card["evaluation_metrics"]["metrics"]["wape_pct"] <= 15.0


def test_get_model_card_api():
    r = client.get("/models/card")
    assert r.status_code == 200
    data = r.json()
    assert data["model_details"]["version"] == "1.0.0"
    assert len(data["training_data"]["dataset_sources"]) >= 5
