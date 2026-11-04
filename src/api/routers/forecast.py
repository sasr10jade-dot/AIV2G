"""AI 수요·공급 예측 서빙 API — ㈜아크릴 예측 결과 우선, 없으면 기준(baseline) 모델."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from src.auth.deps import require_admin
from src.datagen.config import SimConfig
from src.model.forecast_report import build_forecast_report_docx, build_forecast_report_pdf
from src.model.forecast_service import GRANULARITY_PRESETS, forecast_latest, forecast_preferred
from src.model.plugin_pattern import InsufficientHistoryError, UnknownVehicleError, forecast_plugin_schedule
from src.pipeline.source import load_scenario

router = APIRouter(prefix="/forecast", tags=["forecast"], dependencies=[Depends(require_admin)])

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF_MEDIA_TYPE = "application/pdf"


@router.get("/{target_col}", summary="최근 구간 실측 vs 예측 (모델 적합도 확인용)")
def get_forecast(
    target_col: str,
    periods: int = Query(24, ge=1, le=500, description="반환할 시간 수(홀드아웃 구간 기준)"),
) -> dict:
    """등록된 활성 모델로 최근 구간의 실측값·예측값을 함께 반환한다. 미래 예측이
    아니라 모델이 과거 구간을 얼마나 잘 맞추는지(적합도) 보기 위한 용도다 —
    미래 구간 전망은 `/forecast/{target_col}/horizon`을 쓴다."""
    try:
        result = forecast_latest(target_col, periods=periods)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "target_col": result.target_col,
        "model_version": result.model_version,
        "is_real_data": result.is_real_data,
        "timestamps": result.timestamps,
        "y_actual": result.y_actual,
        "y_pred": result.y_pred,
    }


@router.get("/{target_col}/horizon", summary="일별(시간대별)/주별/월별 미래 전망 — ㈜아크릴 예측 우선, 없으면 기준 모델 재귀 예측")
def get_forecast_horizon(
    target_col: str,
    granularity: str = Query(
        "hourly", pattern="^(hourly|daily|weekly)$",
        description="hourly=다음 48시간(시간단위) · daily=다음 4주(일 합계) · weekly=다음 약 6개월(주 합계)",
    ),
) -> dict:
    """일별(시간대별)/주별/월별 조회 단위로 미래 구간 전망을 낸다. 시간대별(48시간)은 ㈜아크릴이 보낸 예측이 24시간 이상
    있으면 그것을 쓰고(`model_source="acryl"`), 아니면 기준 모델로 재귀 예측한다(`model_source="baseline"`).

    - hourly: 다음 48시간, 시간 단위
    - daily: 다음 4주, 일 합계(kWh)
    - weekly: 다음 약 6개월, 주 합계(kWh) — 정밀 예측이 아닌 추세 전망

    **실제 데이터 연동 시 유의**: 지금 모델은 합성 데이터(규칙적인 요일·시간대
    패턴)로 학습돼 예측이 매끄럽다. 실제 차량 운행은 이보다 불규칙할 가능성이
    높아, 실증 데이터로 재학습하면 예측 오차(특히 daily/weekly)가 지금 데모보다
    커지거나 변동성이 커질 수 있다 — Closed-loop 재학습 루프가 이걸 감지·대응
    하도록 설계돼 있지만(`/dashboard/data`의 `closed_loop`), 절대적인 정확도
    수치는 실증 데이터 양·품질에 따라 달라진다.
    """
    try:
        return forecast_preferred(target_col, granularity)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{target_col}/horizon/report", summary="일/주/월 전망 리포트 다운로드 (Word .docx 또는 PDF)")
def download_forecast_report(
    target_col: str,
    granularity: str = Query("hourly", pattern="^(hourly|daily|weekly)$"),
    format: str = Query("docx", pattern="^(docx|pdf)$", description="'docx'(Word) 또는 'pdf'"),
) -> Response:
    """`/forecast/{target_col}/horizon`과 동일한 재귀 예측 결과를 요약·차트·상세표가
    포함된 문서로 내려받는다 — 화면에서 본 값과 항상 일치한다. Word(.docx)와 PDF 모두
    같은 예측 결과를 재사용하므로 두 포맷의 수치는 항상 일치한다."""
    try:
        if format == "pdf":
            content = build_forecast_report_pdf(target_col, granularity)
            media_type, ext = PDF_MEDIA_TYPE, "pdf"
        else:
            content = build_forecast_report_docx(target_col, granularity)
            media_type, ext = DOCX_MEDIA_TYPE, "docx"
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    ascii_name = f"forecast_report_{granularity}.{ext}"
    display_name = f"AI수요예측전망_{target_col}_{granularity}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(display_name)}"},
    )


@router.get("/plugin-pattern/{ev_id}", summary="차량별 입출차(플러그인/아웃) 패턴 예측 — 요일·시간대 이력 기반")
def get_plugin_pattern(
    ev_id: str,
    horizon_hours: int = Query(48, ge=1, le=168, description="예측할 시간 수(기본 다음 48시간)"),
) -> dict:
    """새 ML 모델을 학습하지 않고, 그 차량의 과거 (요일, 시간대)별 연결 빈도를 그대로
    다음 `horizon_hours`에 투영한다 — 출퇴근처럼 반복되는 생활 패턴은 매주 재학습할
    근거가 없고 이력 집계로 충분히 설명되기 때문이다. 최소 2주 이력이 필요하며, 그
    전까지는 `/ingest/obd-emulator/run`으로 여러 주 분량을 합성해 검증할 수 있다.

    **실제 데이터 연동 시 유의**: 이 방식은 "그 차량이 요일·시간대별로 얼마나
    일관되게 행동하는가"에 정확도가 전적으로 좌우된다 — 합성 데이터는 출퇴근
    패턴이 규칙적이라 예측이 잘 맞지만, 실제 운전자는 요일마다 다르게 움직일 수
    있어 실증 초기(이력 2~4주 수준)엔 예측 신뢰도가 차량마다 크게 다를 수 있다.
    이력이 쌓일수록(수개월) 개선된다."""
    cfg = SimConfig()
    ev_df, _charger_df, _grid_df, is_real = load_scenario(cfg)
    try:
        result = forecast_plugin_schedule(ev_df, ev_id, horizon_hours=horizon_hours)
    except UnknownVehicleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result["is_real_data"] = is_real
    return result


@router.get("/meta/granularities", summary="지원하는 조회 단위(granularity) 목록")
def list_granularities() -> dict:
    """`/forecast/{target_col}/horizon`에 쓸 수 있는 granularity 값과 각각의
    horizon_hours·집계 단위(bucket)를 반환한다."""
    return GRANULARITY_PRESETS
