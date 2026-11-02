"""알림 발송 관리 API(관리자) — 차량 등록자 알림함으로 알림을 보낸다.

수신 동의가 없는 차량에는 저장되지 않는다(`sent=false`). 자동 생성(충전 시간대·수요예측·추천)은
이후 단계에서 같은 `notify.create`를 쓴다.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.auth.deps import require_admin
from src.notify import service as notify
from src.privacy import audit

router = APIRouter(prefix="/notify", tags=["notify"], dependencies=[Depends(require_admin)])


class SendIn(BaseModel):
    ev_id: str = Field(..., min_length=1, max_length=50)
    kind: str = "system"
    title: str = Field(..., min_length=1, max_length=notify.MAX_TITLE)
    body: str = Field(..., min_length=1, max_length=notify.MAX_BODY)
    dedupe_key: str | None = Field(None, max_length=100)
    title_en: str | None = Field(None, max_length=notify.MAX_TITLE, description="영어 제목(선택) — 영어 앱에서 보인다")
    body_en: str | None = Field(None, max_length=notify.MAX_BODY, description="영어 내용(선택)")


@router.post("/send", summary="차량 알림함으로 알림 발송(수신 동의 차량만)")
def send(body: SendIn, user: dict = Depends(require_admin)) -> dict:
    try:
        payload = {"en": {"title": body.title_en.strip(), "body": body.body_en.strip()}} if body.title_en and body.body_en else None
        notification_id = notify.create(body.ev_id, body.kind, body.title, body.body, payload=payload, dedupe_key=body.dedupe_key)
    except notify.NotifyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    audit.log(user["username"], "notify_send", target=body.ev_id,
              detail={"kind": body.kind, "sent": notification_id is not None})
    return {"sent": notification_id is not None, "notification_id": notification_id}
