"""데이터 연계·적재 API (WS1) — 데모(합성) 데이터 생성과 실제 실증 데이터 업로드를
모두 지원한다. 업로드가 성공하면 이후 /forecast, /optimize, /dashboard가 자동으로
그 데이터를 사용한다(`src/pipeline/source.py`가 소스 전환을 감춘다).
"""
import io

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from src.automation import issues
from src.timeutil import kst_now
from src.auth.deps import require_admin
from src.datagen import generate
from src.datagen.config import SimConfig
from src.datagen.ev import VEHICLE_MODELS
from src.pipeline import ingest as ingest_pipeline
from src.pipeline import schema as schema_module
from src.pipeline import source
from src.datagen.grid import generate_grid_signal
from src.pipeline.kpx_ingest import KpxApiError, KpxNotConfigured, fetch_smp_hourly, merge_smp_into_grid_signal
from src.datagen.obd_emulator import generate_synthetic_obd_day
from src.pipeline.obd_ingest import ObdColumnsMissingError, aggregate_obd_day_to_hourly, merge_hourly_rows_into_source
from src.pipeline.quality import summarize, validate
from src.pipeline import public_csv, weather_holiday
from src.pipeline.public_api import ApiNotConfigured, ApiResponseError

router = APIRouter(prefix="/ingest", tags=["ingest"], dependencies=[Depends(require_admin)])


@router.post("/generate", summary="합성 데모 데이터 생성")
def generate_raw_data() -> dict:
    """합성 실증 데이터를 raw CSV로 생성한다(데모 모드)."""
    return generate.run(SimConfig())


@router.post("/run", summary="raw → curated 파이프라인 실행")
def run_ingest_pipeline() -> dict:
    """raw CSV -> 표준화·품질검증 -> curated 저장까지 파이프라인을 실행한다."""
    try:
        result = ingest_pipeline.run(SimConfig())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {k: v for k, v in result.items() if k != "quality_report_md"}


@router.get("/status", summary="현재 데이터 소스 모드 확인 (데모/실증)")
def ingest_status() -> dict:
    """현재 /forecast, /optimize, /dashboard가 어떤 데이터를 쓰고 있는지 확인한다."""
    return {"mode": source.current_mode(), "has_raw_data": source.has_raw_data()}


@router.post("/upload", summary="실증 데이터 업로드 (품질검증 통과 시 반영)")
async def upload_real_data(
    ev_telemetry: UploadFile = File(..., description="EV_TELEMETRY_SCHEMA 형식 CSV"),
    charger_sessions: UploadFile = File(..., description="CHARGER_SESSION_SCHEMA 형식 CSV — ev_id(선택)를 채우면 충전 효율 분석(/dashboard/charging-efficiency) 대상이 됨"),
    grid_signal: UploadFile = File(..., description="GRID_SIGNAL_SCHEMA 형식 CSV"),
) -> dict:
    """실제 실증 데이터를 업로드한다. 품질검증(WS1 기준)을 통과해야 반영되며,
    반영되면 이후 예측·최적화·대시보드가 합성 데모 데이터 대신 이 데이터를 쓴다.

    `ev_telemetry` CSV에는 스키마 필수 컬럼 외에 선택 컬럼도 넣을 수 있다 —
    `vehicle_model`/`battery_capacity_kwh`/`v2g_capable`(차종별 V2G 배분 로직에
    사용)와 `batt_temp_c`(대시보드 배터리온도 표시·이상 감지용). 없으면 각각
    기본값(전역 배터리 용량, V2G 가능, 온도 미표시)으로 대체된다."""
    try:
        ev_df = pd.read_csv(io.BytesIO(await ev_telemetry.read()), parse_dates=["timestamp"])
        charger_df = pd.read_csv(io.BytesIO(await charger_sessions.read()), parse_dates=["start_ts"])
        grid_df = pd.read_csv(io.BytesIO(await grid_signal.read()), parse_dates=["timestamp"])
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"CSV 파싱 실패: {exc}") from exc

    reports = [
        validate(ev_df, "ev_telemetry", schema_module.EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"]),
        validate(charger_df, "charger_sessions", schema_module.CHARGER_SESSION_SCHEMA, dedup_keys=["session_id"]),
        validate(grid_df, "grid_signal", schema_module.GRID_SIGNAL_SCHEMA, dedup_keys=["timestamp"]),
    ]
    if not all(r.passed for r in reports):
        raise HTTPException(status_code=422, detail={"quality_report_md": summarize(reports)})

    ev_df.to_csv(source.EV_CSV, index=False)
    charger_df.to_csv(source.CHARGER_CSV, index=False)
    grid_df.to_csv(source.GRID_CSV, index=False)
    source.mark_source("uploaded")

    return {"status": "ok", "mode": "uploaded", "quality_report_md": summarize(reports)}


@router.post("/upload-obd", summary="실증 차량 OBD 배치 업로드 (원본 1Hz 로그 → 시간당 자동 집계)")
async def upload_obd_batch(
    ev_id: str = Form(..., description="차량 식별자 (예: EV-TORRES-01)"),
    vehicle_model: str = Form(..., description=f"차종 (선택지: {', '.join(VEHICLE_MODELS)})"),
    zone: int = Form(0, ge=0, description="차량이 속한 구역 번호"),
    files: list[UploadFile] = File(..., description="원본 OBD CSV 1개 이상 (배치 전송 — 하루 1회 등)"),
) -> dict:
    """실증 차량에 필수 장착된 OBD가 실시간이 아니라 배치(예: 일 단위)로 보내는 원본
    1Hz 로그를 받아 시간당 `ev_telemetry` 행으로 자동 집계하고 기존 실증 데이터에
    병합한다. 파일마다 컬럼 구성이 달라도(스키마 드리프트, `OBDData/` 실측 279개
    파일에서 실제로 확인된 현상) 안전하게 처리하며, 처리 실패한 파일은 건너뛰고
    사유를 함께 보고한다(전체 요청을 실패시키지 않음 — 배치 중 일부만 이상이어도
    나머지는 반영되어야 한다).

    충전기·계통 데이터가 아직 없어도(충전사업자·공공데이터 연계 전 단계) 이 EV
    데이터만으로 실증 모드에 진입한다 — `src/pipeline/source.py`가 부족한 부분을
    EV 실측 규모에 맞춘 합성 데이터로 보완한다."""
    if vehicle_model not in VEHICLE_MODELS:
        raise HTTPException(status_code=422, detail=f"알 수 없는 차종입니다: {vehicle_model} (선택지: {', '.join(VEHICLE_MODELS)})")

    all_rows = []
    file_reports = []
    for f in files:
        try:
            raw = await f.read()
            day_df = pd.read_csv(io.BytesIO(raw), low_memory=False)
            hourly = aggregate_obd_day_to_hourly(day_df, ev_id=ev_id, vehicle_model=vehicle_model, zone=zone)
            all_rows.append(hourly)
            file_reports.append({"file": f.filename, "status": "ok", "rows": len(hourly)})
        except (ObdColumnsMissingError, ValueError, KeyError) as exc:
            file_reports.append({"file": f.filename, "status": "error", "detail": str(exc)})

    if not all_rows:
        raise HTTPException(status_code=422, detail={"file_reports": file_reports, "message": "처리 가능한 파일이 없습니다"})

    new_ev_df = pd.concat(all_rows, ignore_index=True)
    merge_result = merge_hourly_rows_into_source(new_ev_df)

    return {"status": "ok", "mode": "uploaded", "file_reports": file_reports, **merge_result}


@router.post("/obd-emulator/run", summary="가상 실증차량 OBD 배치를 합성해 실제 파이프라인으로 흘려보냄")
def run_obd_emulator(
    n_vehicles: int = 5,
    n_days: int = 3,
    inject_messiness: bool = True,
    seed: int = 0,
) -> dict:
    """실물 차량 40대·5개 차종이 다 모이기 전까지 배치 수집 파이프라인을 규모감
    있게 검증하기 위한 에뮬레이터 — OCPP 에뮬레이터와 같은 이유다. `n_vehicles`대를
    5개 차종에 순환 배정해 각각 `n_days`일치 원본 OBD를 합성하고, **실제 운영에서
    쓰는 것과 동일한 코드 경로**(`aggregate_obd_day_to_hourly` → `merge_hourly_rows_into_source`)로
    흘려보낸다 — 나중에 진짜 OBD 데이터로 바꿀 때 이 엔드포인트를 지우고
    `/ingest/upload-obd`(실제 업로드)만 남기면 되고, 파이프라인 코드는 그대로 재사용된다.

    `inject_messiness=True`(기본)면 실측(`OBDData/`)에서 확인된 스키마 드리프트·
    BMS_SOC 스케일 오류·이상(degraded) 로그까지 재현해 강건성을 계속 검증한다."""
    if n_vehicles < 1 or n_vehicles > 200:
        raise HTTPException(status_code=422, detail="n_vehicles는 1~200 사이여야 합니다")
    if n_days < 1 or n_days > 30:
        raise HTTPException(status_code=422, detail="n_days는 1~30 사이여야 합니다")

    rng = np.random.default_rng(seed)
    models = list(VEHICLE_MODELS.keys())
    base_date = kst_now().normalize() - pd.Timedelta(days=n_days)

    all_rows = []
    file_reports = []
    for i in range(n_vehicles):
        ev_id = f"EV-EMU-{i + 1:03d}"
        vehicle_model = models[i % len(models)]
        zone = i % 4
        for d in range(n_days):
            date = base_date + pd.Timedelta(days=d)
            try:
                day_df = generate_synthetic_obd_day(vehicle_model, date, rng, inject_messiness=inject_messiness)
                hourly = aggregate_obd_day_to_hourly(day_df, ev_id=ev_id, vehicle_model=vehicle_model, zone=zone)
                all_rows.append(hourly)
                file_reports.append({"ev_id": ev_id, "date": date.strftime("%Y-%m-%d"), "status": "ok", "rows": len(hourly)})
            except ObdColumnsMissingError as exc:
                # inject_messiness=True면 이상(degraded) 로그가 의도적으로 섞여 있어 실패가
                # "정상적으로 예상된" 결과다 — 실제 배치 업로드와 똑같이 건너뛰고 계속 진행한다.
                file_reports.append({"ev_id": ev_id, "date": date.strftime("%Y-%m-%d"), "status": "error", "detail": str(exc)})

    if not all_rows:
        raise HTTPException(status_code=422, detail={"file_reports": file_reports, "message": "생성된 유효 데이터가 없습니다"})

    new_ev_df = pd.concat(all_rows, ignore_index=True)
    merge_result = merge_hourly_rows_into_source(new_ev_df)

    degraded_count = sum(1 for r in file_reports if r["status"] == "error")
    return {
        "status": "ok",
        "mode": "uploaded",
        "n_vehicles": n_vehicles,
        "n_days": n_days,
        "degraded_batches": degraded_count,
        "file_reports": file_reports,
        **merge_result,
    }


@router.post("/sync-kpx-smp", summary="한국전력거래소(KPX) 실시간 SMP를 grid_signal.price_krw_per_kwh에 반영")
def sync_kpx_smp(area: str = "mainland") -> dict:
    """공공데이터포털에 등록된 KPX 계통한계가격조회 API에서 오늘자 시간별 SMP를 받아
    기존 `grid_signal` 데이터의 `price_krw_per_kwh`를 실측으로 덮어쓴다.

    SMP는 전국 단일 정산가격(육지/제주만 구분)이라 `power_demand_mw`/`renewable_gen_mw`
    처럼 지역 스케일 문제가 없다 — 도시형 모델에 그대로 반영해도 되는 유일한 grid_signal
    필드다. `KPX_SERVICE_KEY` 환경변수가 없으면 503을 반환한다(.env 설정 필요, 공공데이터
    포털에서 API 활용신청 후 발급 — 개발단계도 심의승인 필요).

    **한계**: 매번 호출 시 "오늘자"만 덮어쓰고 과거 이력을 누적 저장하지 않는다 —
    그래서 지금은 가격 신호 보정용이며, SMP 자체를 예측하는 모델(별도 학습 대상)을
    만들려면 이 호출 결과를 매일 누적하는 저장 로직이 먼저 필요하다(아직 없음)."""
    try:
        smp_df = fetch_smp_hourly(area=area)
    except KpxNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KpxApiError as exc:
        issues.record("public_data", f"전력거래소 SMP 호출 실패: {exc}")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if source.GRID_CSV.exists():
        grid_df = pd.read_csv(source.GRID_CSV, parse_dates=["timestamp"])
    else:
        # grid_signal이 아직 없으면(데모 모드) 합성 기준선을 하나 만들어두고 그 위에
        # 실측 SMP만 덮어쓴다 — power_demand_mw/renewable_gen_mw는 여전히 합성값이라는
        # 점을 응답에 명시한다.
        rng = np.random.default_rng(SimConfig().random_seed)
        grid_df = generate_grid_signal(SimConfig(), rng)

    merged = merge_smp_into_grid_signal(grid_df, smp_df)
    merged.to_csv(source.GRID_CSV, index=False)
    if not source.EV_CSV.exists():
        # EV 데이터 없이 grid_signal만 있으면 has_raw_data()가 False라 반영되지 않는다 —
        # 이 상태를 명확히 알린다(현재는 OBD 업로드가 실증 모드 진입의 최소 조건).
        note = "EV 실증 데이터가 아직 없어 이 grid_signal은 저장만 되고 대시보드에는 아직 반영되지 않습니다(/ingest/upload-obd 선행 필요)."
    else:
        source.mark_source("uploaded")
        note = "반영 완료 — 이후 /dashboard, /forecast가 이 SMP를 사용합니다."

    return {
        "status": "ok",
        "area": area,
        "synced_hours": len(smp_df),
        "note": note,
    }


@router.get("/public-apis/status", summary="공개 데이터(기상·공휴일) 연동 준비 상태 — 키·명세 여부만, 값은 노출하지 않음")
def public_api_status() -> dict:
    return {"apis": weather_holiday.status()}


@router.post("/sync-weather-holiday", summary="기상청 시간별 기온·한국천문연구원 공휴일을 grid_signal에 반영")
def sync_weather_holiday(start: str, end: str, station: str = weather_holiday.DEFAULT_STATION) -> dict:
    """계획서 4.1 가 '외부환경(기상·특일)' 공개 데이터를 받아 `grid_signal`의 `temperature_c`·`is_holiday`를 덮어쓴다.
    기간은 날짜(YYYY-MM-DD)로 받고 최대 92일이다. 기온은 전날까지만 제공된다. `DATA_GO_KR_SERVICE_KEY`가 없으면 503."""
    try:
        d0, d1 = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="start·end는 YYYY-MM-DD 형식이어야 합니다") from exc
    if d1 < d0 or (d1 - d0).days > 91:
        raise HTTPException(status_code=422, detail="기간은 시작일 이후 최대 92일까지입니다")
    try:
        temp = weather_holiday.fetch_hourly_temperature(d0, d1, station=station)
        hol = weather_holiday.fetch_holidays(d0, d1)
    except ApiNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (ApiResponseError, OSError) as exc:
        issues.record("public_data", f"기상·공휴일 공개 데이터 호출 실패: {type(exc).__name__}", detail={"error": str(exc)[:200]})
        raise HTTPException(status_code=502, detail=f"공개 데이터 호출 실패: {exc}") from exc

    if source.GRID_CSV.exists():
        grid_df = pd.read_csv(source.GRID_CSV, parse_dates=["timestamp"])
    else:
        rng = np.random.default_rng(SimConfig().random_seed)
        grid_df = generate_grid_signal(SimConfig(), rng)
    merged, stats = weather_holiday.merge_into_grid_signal(grid_df, temp, hol, d0, d1)
    merged.to_csv(source.GRID_CSV, index=False)
    if source.EV_CSV.exists():
        source.mark_source("uploaded")
        note = "반영 완료 — 이후 /dashboard, /forecast가 이 기온·공휴일을 사용합니다."
    else:
        note = "EV 실증 데이터가 아직 없어 grid_signal에 저장만 했습니다(대시보드 반영은 /ingest/upload-obd 이후)."
    return {"status": "ok", "station": station, "fetched_hours": len(temp), **stats, "note": note}


@router.post("/public-csv/{kind}", summary="공개 데이터 CSV 반영 — 시간별 전력수요(power-demand)·재생에너지 발전량(renewable)")
async def upload_public_csv(kind: str, file: UploadFile = File(..., description="공공데이터포털에서 받은 시간별 CSV(가로형 1~24시 또는 세로형)"),
                            region: str | None = Form(None, description="세로형에 지역 열이 있으면 거를 지역(예: 전남)")) -> dict:
    """계획서 4.1 가 '시간대별 전력수요·재생에너지 공개 데이터'를 grid_signal에 반영한다. 시각이 맞는 행만 덮어쓰고 나머지는 그대로 둔다.
    값의 단위는 바꾸지 않는다(전국 수요라면 도시 규모보다 크다 — 피크 시간대는 날짜별 상위 25%라 규모와 무관)."""
    column = public_csv.KINDS.get(kind)
    if column is None:
        raise HTTPException(status_code=404, detail="kind는 power-demand 또는 renewable 입니다")
    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="20MB 이하 파일만 올릴 수 있습니다")
    try:
        series = public_csv.parse_hourly(content, region=(region or "").strip() or None)
    except public_csv.CsvFormatError as exc:
        issues.record("public_data", f"공개 데이터 CSV 형식 오류: {exc}", kind="data", detail={"kind": kind})
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if source.GRID_CSV.exists():
        grid_df = pd.read_csv(source.GRID_CSV, parse_dates=["timestamp"])
    else:
        rng = np.random.default_rng(SimConfig().random_seed)
        grid_df = generate_grid_signal(SimConfig(), rng)
    merged, n = public_csv.merge_into_grid(grid_df, series, column)
    merged.to_csv(source.GRID_CSV, index=False)
    if source.EV_CSV.exists():
        source.mark_source("uploaded")
    note = ("반영할 시각이 겹치지 않았습니다 — CSV 기간이 실증 데이터 기간과 같은지 확인해 주세요." if n == 0 else
            "반영 완료 — 이후 /dashboard, /forecast가 이 값을 사용합니다." if source.EV_CSV.exists() else
            "EV 실증 데이터가 아직 없어 grid_signal에 저장만 했습니다(대시보드 반영은 /ingest/upload-obd 이후).")
    return {"status": "ok", "column": column, "rows_in_file": len(series), "rows_applied": n,
            "period": [series["timestamp"].min().isoformat(), series["timestamp"].max().isoformat()], "note": note}


@router.delete("/upload", summary="업로드된 데이터 삭제 → 데모 모드로 복귀")
def reset_to_demo_data() -> dict:
    """업로드된 실증 데이터를 지우고 데모(합성) 모드로 되돌린다."""
    source.clear_raw_data()
    return {"status": "ok", "mode": "demo"}
