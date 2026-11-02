"""OBD 일일 배치 운영 API — 상태 조회·수동 실행·승격. 실제 새벽 실행은 스케줄러가
`scripts/run_obd_batch.py`를 호출한다(이 API는 점검·재실행용)."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from src.auth.deps import require_admin, require_super_admin
from src.pipeline import batch_ingest as b
from src.privacy import audit

router = APIRouter(prefix="/ingest/batch", tags=["ingest"])


@router.get("/status", summary="최근 OBD 일일 배치 결과(도착 대조·격리·모드)", dependencies=[Depends(require_admin)])
def batch_status() -> dict:
    report = b.latest_report()
    return {"has_report": report is not None, "report": report}


@router.post("/run", summary="OBD 일일 배치 즉시 실행(백그라운드)")
def batch_run(background: BackgroundTasks, user: dict = Depends(require_admin)) -> dict:
    audit.log(user["username"], "batch_run_manual")
    background.add_task(b.run_daily_batch)
    return {"status": "started"}


@router.post("/promote", summary="shadow로 검증한 staging 결과를 운영 데이터에 반영(최고관리자)")
def batch_promote(ev_ids: str | None = None, user: dict = Depends(require_super_admin)) -> dict:
    audit.log(user["username"], "batch_promote", detail={"ev_ids": ev_ids})
    ids = {x.strip() for x in ev_ids.split(",") if x.strip()} if ev_ids else None
    try:
        return b.promote_staged(b.make_paths(), ids)
    except Exception as exc:  # 반영 실패는 운영 파일을 건드리지 않고 사유를 반환한다
        raise HTTPException(status_code=500, detail=f"승격 실패: {exc}") from exc
