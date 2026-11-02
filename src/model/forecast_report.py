"""AI 수요예측 전망(일별/주별/월별) Word(.docx) 리포트 생성.

대시보드의 "AI 수요예측 전망" 카드 다운로드 버튼이 호출한다. `forecast_horizon()`이
계산한 결과(재귀 예측)를 그대로 받아 요약·차트·상세표가 포함된 문서로 만든다 —
새로 계산하지 않으므로 대시보드에서 본 값과 다운로드한 문서의 값이 항상 일치한다.
"""
import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

plt.rcParams.update({"font.family": "Malgun Gothic", "axes.unicode_minus": False})  # src/viz.py와 동일 — 한글(예: "시") 깨짐 방지
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image as RLImage
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from src.timeutil import kst_now
from src.datagen.config import SimConfig
from src.model.forecast_service import GRANULARITY_PRESETS, forecast_horizon

TEAL = "#00897B"  # src/viz.py와 동일한 "AI 예측" 시맨틱 색상
GRID_LINE = "#DCE3E7"
WARN = RGBColor(0xB4, 0x7B, 0x00)
MUTED = RGBColor(0x6B, 0x77, 0x85)

# PDF는 한글이 포함된 문서라 Windows 기본 한글 서체(맑은 고딕)를 직접 임베드한다 —
# reportlab 기본 서체(Helvetica)는 한글 글리프가 없어 문서가 아예 깨진다.
PDF_FONT, PDF_FONT_BOLD = "Helvetica", "Helvetica-Bold"
try:
    pdfmetrics.registerFont(TTFont("MalgunGothic", r"C:\Windows\Fonts\malgun.ttf"))
    pdfmetrics.registerFont(TTFont("MalgunGothic-Bold", r"C:\Windows\Fonts\malgunbd.ttf"))
    PDF_FONT, PDF_FONT_BOLD = "MalgunGothic", "MalgunGothic-Bold"
except Exception:
    pass  # 폰트 파일이 없는 환경(비-Windows)에서는 영문만 정상 표시된다

UNIT_BY_GRANULARITY = {"hourly": "kW", "daily": "kWh", "weekly": "kWh"}
DATE_LABEL_BY_GRANULARITY = {"hourly": "시각", "daily": "날짜", "weekly": "주 시작일"}


def _render_chart_png(result: dict) -> bytes:
    timestamps = pd.to_datetime(result["timestamps"])
    values = result["values"]
    unit = UNIT_BY_GRANULARITY.get(result["granularity"], "")

    fig, ax = plt.subplots(figsize=(7.2, 3.0), dpi=150)
    ax.plot(timestamps, values, color=TEAL, linewidth=2)
    ax.fill_between(timestamps, values, color=TEAL, alpha=0.12)
    ax.set_ylabel(unit)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color=GRID_LINE, linewidth=0.7)
    ax.set_axisbelow(True)
    date_fmt = "%m/%d %H시" if result["bucket"] == "hour" else "%m/%d"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(date_fmt))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


def build_forecast_report_docx(target_col: str, granularity: str, cfg: SimConfig | None = None) -> bytes:
    """target_col·granularity에 대해 재귀 예측을 수행하고 Word 리포트 바이트를 반환한다."""
    result = forecast_horizon(target_col, granularity, cfg=cfg)
    preset = GRANULARITY_PRESETS[granularity]
    unit = UNIT_BY_GRANULARITY.get(granularity, "")
    date_label = DATE_LABEL_BY_GRANULARITY.get(granularity, "시점")

    timestamps = result["timestamps"]
    values = result["values"]
    total = sum(values)
    avg = total / len(values) if values else 0.0
    peak = max(values) if values else 0.0

    doc = Document()

    doc.add_heading("AI 수요예측 전망 리포트", level=0)

    meta = doc.add_paragraph()
    meta_run = meta.add_run(
        f"예측대상: {target_col}   ·   조회단위: {preset['label']}   ·   "
        f"생성일시: {kst_now().strftime('%Y-%m-%d %H:%M')}"
    )
    meta_run.font.size = Pt(10)
    meta_run.font.color.rgb = MUTED

    note = doc.add_paragraph()
    note_run = note.add_run(
        "※ 재귀 예측(recursive forecasting) — 모델이 자신의 이전 예측을 다시 입력으로 사용해 "
        '미래로 걸어나가는 방식이라, 기간이 길어질수록(특히 "월별") 오차가 누적됩니다. '
        "정밀 예측이 아니라 추세 전망으로 참고하시기 바랍니다. "
        "본 리포트의 수치는 AI 예측·추론 값이며 실측값 또는 실제 V2G 방전 실적이 아닙니다."
    )
    note_run.italic = True
    note_run.font.size = Pt(9)
    note_run.font.color.rgb = WARN

    doc.add_heading("요약", level=1)
    summary = doc.add_table(rows=2, cols=3)
    summary.style = "Light Grid Accent 1"
    hdr, row = summary.rows[0].cells, summary.rows[1].cells
    hdr[0].text, hdr[1].text, hdr[2].text = "합계", "평균", "최대"
    row[0].text, row[1].text, row[2].text = f"{total:,.1f} {unit}", f"{avg:,.2f} {unit}", f"{peak:,.2f} {unit}"

    doc.add_heading("전망 차트", level=1)
    doc.add_picture(io.BytesIO(_render_chart_png(result)), width=Cm(16))

    doc.add_heading("상세 데이터", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.style = "Light List Accent 1"
    hdr = table.rows[0].cells
    hdr[0].text, hdr[1].text = date_label, f"예측값 ({unit})"
    date_fmt = "%Y-%m-%d %H:%M" if result["bucket"] == "hour" else "%Y-%m-%d"
    for ts, v in zip(timestamps, values):
        cells = table.add_row().cells
        cells[0].text = pd.Timestamp(ts).strftime(date_fmt)
        cells[1].text = f"{v:,.2f}"
        for cell in cells:
            cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.LEFT

    footer = doc.add_paragraph()
    footer_run = footer.add_run("AI기반 도시형 에너지 이익공유 및 전기차 V2G기술 실증 · AI-V2G 실증 대시보드")
    footer_run.font.size = Pt(8)
    footer_run.font.color.rgb = MUTED

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.getvalue()


def build_forecast_report_pdf(target_col: str, granularity: str, cfg: SimConfig | None = None) -> bytes:
    """target_col·granularity에 대해 재귀 예측을 수행하고 PDF 리포트 바이트를 반환한다.

    docx 버전과 동일한 재귀 예측 결과를 재사용하므로 두 포맷의 값은 항상 일치한다."""
    result = forecast_horizon(target_col, granularity, cfg=cfg)
    preset = GRANULARITY_PRESETS[granularity]
    unit = UNIT_BY_GRANULARITY.get(granularity, "")
    date_label = DATE_LABEL_BY_GRANULARITY.get(granularity, "시점")

    timestamps = result["timestamps"]
    values = result["values"]
    total = sum(values)
    avg = total / len(values) if values else 0.0
    peak = max(values) if values else 0.0

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleKo", parent=styles["Title"], fontName=PDF_FONT_BOLD, fontSize=19, spaceAfter=4,
    )
    h2_style = ParagraphStyle(
        "H2Ko", parent=styles["Heading2"], fontName=PDF_FONT_BOLD, fontSize=13,
        textColor=colors.HexColor("#0B0B0B"), spaceBefore=14, spaceAfter=6,
    )
    # python-docx RGBColor는 reportlab Color가 아니므로 (r,g,b) 튜플로 직접 변환한다
    meta_style = ParagraphStyle(
        "MetaKo", parent=styles["Normal"], fontName=PDF_FONT, fontSize=9.5,
        textColor=colors.Color(MUTED[0] / 255, MUTED[1] / 255, MUTED[2] / 255),
    )
    note_style = ParagraphStyle(
        "NoteKo", parent=styles["Normal"], fontName=PDF_FONT, fontSize=8.5,
        textColor=colors.Color(WARN[0] / 255, WARN[1] / 255, WARN[2] / 255),
    )
    body_style = ParagraphStyle("BodyKo", parent=styles["Normal"], fontName=PDF_FONT, fontSize=9, leading=13)
    footer_style = ParagraphStyle(
        "FooterKo", parent=styles["Normal"], fontName=PDF_FONT, fontSize=8,
        textColor=colors.Color(MUTED[0] / 255, MUTED[1] / 255, MUTED[2] / 255),
    )

    story = [
        Paragraph("AI 수요예측 전망 리포트", title_style),
        Paragraph(
            f"예측대상: {target_col}   ·   조회단위: {preset['label']}   ·   "
            f"생성일시: {kst_now().strftime('%Y-%m-%d %H:%M')}",
            meta_style,
        ),
        Spacer(1, 4),
        Paragraph(
            "<i>※ 재귀 예측(recursive forecasting) — 모델이 자신의 이전 예측을 다시 입력으로 사용해 "
            '미래로 걸어나가는 방식이라, 기간이 길어질수록(특히 "월별") 오차가 누적됩니다. '
            "정밀 예측이 아니라 추세 전망으로 참고하시기 바랍니다.</i>",
            note_style,
        ),
        Paragraph("요약", h2_style),
    ]

    summary_table = Table(
        [["합계", "평균", "최대"], [f"{total:,.1f} {unit}", f"{avg:,.2f} {unit}", f"{peak:,.2f} {unit}"]],
        colWidths=[5.3 * cm] * 3,
    )
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), PDF_FONT),
        ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3EEF2")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor(GRID_LINE)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)

    story.append(Paragraph("전망 차트", h2_style))
    chart_buf = io.BytesIO(_render_chart_png(result))
    story.append(RLImage(chart_buf, width=16 * cm, height=16 * cm * 3.0 / 7.2))

    story.append(Paragraph("상세 데이터", h2_style))
    date_fmt = "%Y-%m-%d %H:%M" if result["bucket"] == "hour" else "%Y-%m-%d"
    detail_rows = [[date_label, f"예측값 ({unit})"]]
    for ts, v in zip(timestamps, values):
        detail_rows.append([pd.Timestamp(ts).strftime(date_fmt), f"{v:,.2f}"])
    detail_table = Table(detail_rows, colWidths=[8 * cm, 8 * cm], repeatRows=1)
    detail_style = [
        ("FONTNAME", (0, 0), (-1, -1), PDF_FONT),
        ("FONTNAME", (0, 0), (-1, 0), PDF_FONT_BOLD),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3EEF2")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor(GRID_LINE)),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i in range(1, len(detail_rows)):
        if i % 2 == 0:
            detail_style.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F5F7F8")))
    detail_table.setStyle(TableStyle(detail_style))
    story.append(detail_table)

    story.append(Spacer(1, 14))
    story.append(Paragraph("AI기반 도시형 에너지 이익공유 및 전기차 V2G기술 실증 · AI-V2G 실증 대시보드", footer_style))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
        title="AI 수요예측 전망 리포트",
    )
    doc.build(story)
    buf.seek(0)
    return buf.getvalue()
