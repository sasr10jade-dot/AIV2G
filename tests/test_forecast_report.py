"""AI 수요예측 전망 Word(.docx)/PDF 리포트 생성 테스트."""
import io

import pytest
from docx import Document
from pypdf import PdfReader

from src.automation.model_registry import ModelRegistry
from src.automation.scheduler import bootstrap
from src.datagen.config import SimConfig
from src.model import forecast_report, forecast_service

TARGET = "ev_charging_demand_kw"


@pytest.fixture
def trained_registry(tmp_path):
    reg = ModelRegistry(TARGET, base_dir=tmp_path)
    cfg = SimConfig(n_ev=5, weeks=6, n_chargers=5, charger_days=42, n_zones=2)
    bootstrap(reg, cfg, TARGET)
    return reg, cfg


def _patch_registry(monkeypatch, reg):
    monkeypatch.setattr(forecast_service, "ModelRegistry", lambda target_col: reg)


def test_report_is_valid_docx_with_expected_sections(monkeypatch, trained_registry):
    reg, cfg = trained_registry
    _patch_registry(monkeypatch, reg)

    content = forecast_report.build_forecast_report_docx(TARGET, "hourly", cfg=cfg)
    assert content[:2] == b"PK"  # docx는 zip 컨테이너

    doc = Document(io.BytesIO(content))
    headings = [p.text for p in doc.paragraphs if p.style.name.startswith(("Title", "Heading"))]
    assert headings[0] == "AI 수요예측 전망 리포트"
    assert "요약" in headings
    assert "전망 차트" in headings
    assert "상세 데이터" in headings

    # 표 2개(요약, 상세) + 이미지 1개가 실제로 들어있어야 함
    assert len(doc.tables) == 2
    assert len(doc.inline_shapes) == 1

    detail_table = doc.tables[1]
    assert len(detail_table.rows) == 1 + 48  # 헤더 + 48시간


def test_report_rejects_unknown_target(trained_registry):
    _reg, cfg = trained_registry
    with pytest.raises(FileNotFoundError):
        forecast_report.build_forecast_report_docx("no_such_target", "hourly", cfg=cfg)


def test_pdf_report_is_valid_pdf_with_expected_content(monkeypatch, trained_registry):
    reg, cfg = trained_registry
    _patch_registry(monkeypatch, reg)

    content = forecast_report.build_forecast_report_pdf(TARGET, "hourly", cfg=cfg)
    assert content[:5] == b"%PDF-"

    reader = PdfReader(io.BytesIO(content))
    assert len(reader.pages) >= 1
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "AI 수요예측 전망 리포트" in full_text
    assert "요약" in full_text
    assert "상세 데이터" in full_text


def test_pdf_report_rejects_unknown_target(trained_registry):
    _reg, cfg = trained_registry
    with pytest.raises(FileNotFoundError):
        forecast_report.build_forecast_report_pdf("no_such_target", "hourly", cfg=cfg)
