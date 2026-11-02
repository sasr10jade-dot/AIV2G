"""웹앱 관리 API(관리자) — 서포터즈 공지사항, 앱 안내 문구.

이용자 모바일 앱은 `/owner/notices`·`/owner/app-content`로 읽는다.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.appadmin import service
from src.auth.deps import require_admin
from src.privacy import audit

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class NoticeIn(BaseModel):
    title: str = Field(..., max_length=service.MAX_TITLE)
    body: str = Field(..., max_length=service.MAX_BODY)
    pinned: bool = False
    published: bool = True
    title_en: str | None = Field(None, max_length=service.MAX_TITLE)
    body_en: str | None = Field(None, max_length=service.MAX_BODY)


class ContentIn(BaseModel):
    ko: str | None = Field(None, max_length=service.MAX_TEXT)
    en: str | None = Field(None, max_length=service.MAX_TEXT)


def _run(fn, *args):
    try:
        return fn(*args)
    except service.AdminError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/notices", summary="공지사항 목록(비공개 포함)")
def list_notices() -> dict:
    return {"notices": service.notices(published_only=False, limit=200)}


@router.post("/notices", summary="공지사항 등록")
def create_notice(body: NoticeIn, user: dict = Depends(require_admin)) -> dict:
    nid = _run(service.notice_create, body.title, body.body, body.pinned, body.published, user["username"], body.title_en, body.body_en)
    audit.log(user["username"], "admin_notice_create", target=str(nid))
    return {"id": nid}


@router.put("/notices/{notice_id}", summary="공지사항 수정(공개·고정 포함)")
def update_notice(notice_id: int, body: NoticeIn, user: dict = Depends(require_admin)) -> dict:
    _run(service.notice_update, notice_id, body.title, body.body, body.pinned, body.published, body.title_en, body.body_en)
    audit.log(user["username"], "admin_notice_update", target=str(notice_id))
    return {"status": "ok"}


@router.delete("/notices/{notice_id}", summary="공지사항 삭제")
def delete_notice(notice_id: int, user: dict = Depends(require_admin)) -> dict:
    _run(service.notice_delete, notice_id)
    audit.log(user["username"], "admin_notice_delete", target=str(notice_id))
    return {"status": "ok"}


@router.get("/app-content", summary="수정할 수 있는 앱 문구와 현재 적용값")
def get_content() -> dict:
    return {"editable": service.EDITABLE_KEYS, "overrides": service.content_overrides()}


@router.put("/app-content/{key}", summary="앱 문구 수정(한국어·영어)")
def put_content(key: str, body: ContentIn, user: dict = Depends(require_admin)) -> dict:
    _run(service.content_set, key, body.ko, body.en, user["username"])
    audit.log(user["username"], "admin_content_set", target=key)
    return {"status": "ok"}


@router.delete("/app-content/{key}", summary="앱 문구를 기본값으로 되돌리기")
def reset_content(key: str, user: dict = Depends(require_admin)) -> dict:
    _run(service.content_reset, key)
    audit.log(user["username"], "admin_content_reset", target=key)
    return {"status": "ok"}
