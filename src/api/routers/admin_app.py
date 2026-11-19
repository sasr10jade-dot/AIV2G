"""웹앱 관리 API(관리자) — 서포터즈 공지사항, 앱 안내 문구, 모델링 예외 기록.

이용자 모바일 앱은 `/owner/notices`·`/owner/app-content`로 읽는다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from src.appadmin import service
from src.appadmin import survey
from src.automation import issues
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


class ResolveIn(BaseModel):
    note: str = Field(..., min_length=2, max_length=300, description="원인과 개선 조치(예: 입력 범위 검사 추가, v3 재학습)")


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


@router.get("/modeling-issues", summary="모델링 예외 기록 — 예측·최적화·수집 단계의 오류·예외상황(계획서 4.1 다)")
def list_modeling_issues(status: str = Query("open", pattern="^(open|resolved|all)$"),
                         stage: str | None = Query(None, max_length=40)) -> dict:
    try:
        items = issues.list_issues(status, stage)
    except issues.IssueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"summary": issues.summary(), "stages": issues.STAGES, "kinds": issues.KINDS, "items": items}


@router.post("/modeling-issues/{issue_id}/resolve", summary="모델링 예외 조치 완료 — 원인·개선 조치를 적어 닫는다")
def resolve_modeling_issue(issue_id: int, body: ResolveIn, user: dict = Depends(require_admin)) -> dict:
    try:
        item = issues.resolve(issue_id, user["username"], body.note)
    except issues.IssueError as exc:
        raise HTTPException(status_code=409 if "이미" in str(exc) else 422, detail=str(exc)) from exc
    audit.log(user["username"], "modeling_issue_resolve", target=str(issue_id), detail={"note": item["resolution_note"]})
    return item


# ---------------------------------------------------------------- 참여주체 VoC 설문(요구사항 Y2E05)
class SurveyQuestionIn(BaseModel):
    type: str = Field(..., pattern="^(scale|choice|text)$", description="scale(1~5점) · choice(객관식) · text(주관식)")
    text: str = Field(..., min_length=1, max_length=200)
    options: list[str] | None = Field(None, description="객관식 선택지 2~8개")
    required: bool | None = None


class SurveyIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=80)
    description: str | None = Field(None, max_length=500)
    questions: list[SurveyQuestionIn] = Field(..., min_length=1, max_length=survey.MAX_QUESTIONS)


def _survey(fn, *args):
    try:
        return fn(*args)
    except survey.SurveyError as exc:
        raise HTTPException(status_code=404 if "찾을 수 없" in str(exc) else 422, detail=str(exc)) from exc


@router.get("/surveys", summary="VoC 설문 목록(응답 수 포함)")
def list_surveys() -> dict:
    return {"surveys": survey.list_all()}


@router.post("/surveys", summary="VoC 설문 만들기 — 공개되면 서포터즈 앱 홈에 표시")
def create_survey(body: SurveyIn, user: dict = Depends(require_admin)) -> dict:
    s = _survey(survey.create, body.title, body.description,
                [q.model_dump(exclude_none=True) for q in body.questions], user["username"])
    audit.log(user["username"], "survey_create", target=str(s["id"]), detail={"questions": len(s["questions"])})
    return s


@router.post("/surveys/{survey_id}/close", summary="VoC 설문 마감")
def close_survey(survey_id: int, user: dict = Depends(require_admin)) -> dict:
    s = _survey(survey.close, survey_id)
    audit.log(user["username"], "survey_close", target=str(survey_id))
    return s


@router.get("/surveys/{survey_id}/results", summary="VoC 설문 집계 — 척도 평균·분포, 객관식 건수, 주관식 목록")
def survey_results(survey_id: int) -> dict:
    return _survey(survey.results, survey_id)


@router.get("/surveys/{survey_id}/results.csv", summary="VoC 설문 응답 원자료(CSV, 차량 ID 없음)")
def survey_results_csv(survey_id: int) -> Response:
    data = _survey(survey.results_csv, survey_id)
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=survey_{survey_id}.csv"})
