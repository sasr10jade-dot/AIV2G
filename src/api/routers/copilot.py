"""AI Gemini(EV Bot) API — Gemini 기반 RAG, 대시보드 채팅 위젯이 호출한다."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.auth.deps import require_admin
from src.copilot.service import CopilotNotConfigured, ask, explain_next_session_plan, explain_v2g_schedule
from src.datagen.config import SimConfig
from src.model.plugin_pattern import InsufficientHistoryError, UnknownVehicleError
from src.model.stat_summary import build_stat_summary
from src.optimizer.economics import EconomicsParams
from src.optimizer.poc_compare import (
    plan_next_session,
    run_v2g_optimization,
    select_session_for_explanation,
    summarize_session_plan,
)
from src.pipeline.source import load_scenario
from src.pipeline.store import build_vehicle_status_table, read_dashboard_run_history

router = APIRouter(prefix="/copilot", tags=["copilot"], dependencies=[Depends(require_admin)])


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class AskRequest(BaseModel):
    question: str = Field(..., description="사용자 질문 (한국어)", examples=["1차년도 KPI 가중치가 어떻게 구성되나요?"])
    dashboard_snapshot: dict | None = Field(
        None, description="프런트엔드가 마지막으로 조회한 /dashboard/data 응답 전체 — 근거 데이터로 함께 전달(선택)"
    )
    ev_id: str | None = Field(
        None, description="질문자의 차량 ID(선택) — 비우면 로그인 계정에 등록된 \"내 차\"(owned_ev_id)를 자동으로 쓴다. "
        "주어지면 그 차량의 실시간 상태·실제 LP 최적화 계획·예측 다음 세션을 함께 근거로 전달해 "
        "개인화 질문에 실제 수치로 답하게 한다"
    )
    history: list[HistoryTurn] | None = Field(
        None, description="최근 대화 이력(선택) — \"그럼 얼마야?\" 같은 후속 질문이 맥락을 이어가게 한다. 최근 2턴만 사용됨"
    )


@router.post("/ask", summary="사업계획 문서 + 현재 시나리오 데이터(+선택적으로 내 차량 상태·계획) 기반 질의응답")
def copilot_ask(req: AskRequest, user: dict = Depends(require_admin)) -> dict:
    """PRD·기술개발전략·사업관리체계 문서 전문과 (있다면) 현재 대시보드 시나리오 데이터를
    프롬프트에 그대로 넣어(stuff 방식 RAG) Gemini에게 질의한다.

    `ev_id`가 주어지면(또는 비어 있고 로그인 계정에 "내 차"가 등록돼 있으면) 그 차량의
    실시간 OBD 상태, `/copilot/explain-schedule`과 동일한 실제 LP 최적화 결과, 예측된
    다음 세션 계획을 함께 컨텍스트에 얹는다 — 세션/이력을 못 찾으면 조용히 생략하고
    일반 질의응답으로 진행한다(사용자 경험상 채팅은 항상 답을 줘야 하므로 404를 내지 않음).

    차량과 무관하게, 대시보드가 실제로 새로 계산했던 과거 실행 이력(`dashboard_run_history`)을
    모아 만든 "통계 요약본"도 함께 얹는다 — "지난번보다 나아졌어?" 같은 추세 질문에 답하기 위함.
    이력이 없으면 조용히 생략한다.

    서버 환경변수 `GEMINI_API_KEY`가 없으면 503을 반환한다(.env 파일 설정 필요).
    """
    cfg = SimConfig()
    run_history = read_dashboard_run_history(limit=20)
    if not run_history.empty:
        run_history = run_history[
            (run_history["n_ev"] == cfg.n_ev) & (run_history["weeks"] == cfg.weeks)
        ].reset_index(drop=True)
    stat_summary = build_stat_summary(run_history)

    ev_id = req.ev_id or user.get("owned_ev_id")

    vehicle_status = None
    vehicle_plan = None
    next_session_plan = None
    if ev_id:
        ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)

        status_rows = [r for r in build_vehicle_status_table(ev_df) if r["ev_id"] == ev_id]
        vehicle_status = status_rows[0] if status_rows else None

        result = run_v2g_optimization(ev_df, grid_df, cfg)
        session_id = select_session_for_explanation(
            result["sessions"], result["schedule"], result["session_ev_map"], ev_id
        )
        if session_id is not None:
            vehicle_plan = summarize_session_plan(
                session_id, result["sessions"], result["schedule"], result["session_ev_map"], result["price_by_time"]
            )
        try:
            next_session_plan = plan_next_session(ev_df, grid_df, cfg, ev_id, economics=EconomicsParams.load())
        except (UnknownVehicleError, InsufficientHistoryError, ValueError):
            next_session_plan = None  # 예측 출차 시각이 없거나 이력이 부족하면 조용히 생략

    history = [h.model_dump() for h in req.history] if req.history else None
    try:
        answer = ask(
            req.question, req.dashboard_snapshot, vehicle_plan, next_session_plan, vehicle_status, history,
            stat_summary,
        )
    except CopilotNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "answer": answer,
        "vehicle_status": vehicle_status,
        "vehicle_plan": vehicle_plan,
        "next_session_plan": next_session_plan,
        "stat_summary": stat_summary,
    }


@router.get("/explain-schedule", summary="AI가 특정 EV 세션의 최적 충전/방전 계획을 자연어로 설명")
def explain_schedule(
    ev_id: str | None = Query(None, description="특정 차량 ID(생략 시 방전량이 가장 큰 세션 자동 선택)"),
    max_charge_kw: float = Query(7.0, gt=0, le=50),
    max_discharge_kw: float = Query(7.0, ge=0, le=50),
    reserve_soc: float = Query(0.20, ge=0, le=0.5),
    target_soc: float = Query(0.90, ge=0.5, le=0.99),
) -> dict:
    """V2G 3대 AI 역할 중 "Agent" 파트 — 예측·최적화(LP)가 이미 계산해둔 실제 스케줄을
    가져와 그 숫자 그대로("몇 시에 충전, 몇 시에 방전해서 얼마 번다") 자연어로 풀어준다.
    수치는 전부 `/optimize/poc-compare`와 동일한 LP 최적화 결과이며 지어내지 않는다."""
    cfg = SimConfig()
    ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)
    result = run_v2g_optimization(
        ev_df, grid_df, cfg,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc, target_soc=target_soc,
    )
    session_id = select_session_for_explanation(result["sessions"], result["schedule"], result["session_ev_map"], ev_id)
    if session_id is None:
        raise HTTPException(status_code=404, detail="설명할 수 있는 V2G 세션이 없습니다(방전 스케줄이 비어 있음)")

    plan = summarize_session_plan(
        session_id, result["sessions"], result["schedule"], result["session_ev_map"], result["price_by_time"]
    )
    try:
        explanation = explain_v2g_schedule(plan)
    except CopilotNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"plan": plan, "explanation": explanation}


@router.get("/explain-next-session", summary="AI가 예측된 다음 세션(내일)의 충전/방전 계획을 미리 안내")
def explain_next_session(
    ev_id: str = Query(..., description="차량 ID"),
    max_charge_kw: float = Query(7.0, gt=0, le=50),
    max_discharge_kw: float = Query(7.0, ge=0, le=50),
    reserve_soc: float = Query(0.20, ge=0, le=0.5),
    target_soc: float = Query(0.90, ge=0.5, le=0.99),
    departure_time: str | None = Query(None, description="사용자 입력 출발시각(ISO). 있으면 예측보다 우선"),
    current_soc_pct: float | None = Query(None, ge=0, le=100, description="사용자 입력 현재 SOC(%)"),
    need_soc_pct: float | None = Query(None, ge=20, le=100, description="출발 시 필요 SOC(%)"),
) -> dict:
    """예: "내일 18시에 출차 예정이면 14~16시에 충전 후 19시 피크에 5kWh 방전해서
    3,200원 수익" 같은 안내를 만드는 엔드포인트 — 출차 시각은 이력 기반 예측
    (`forecast_plugin_schedule`), 충/방전 계획은 그 예측 창을 대상으로 한 실제 LP
    최적화 결과다. 둘 다 지어낸 값이 아니라 실제 계산 결과이며, 아직 일어나지
    않은 예측이라는 점을 설명 문장에서도 예측 어투로 구분한다."""
    cfg = SimConfig()
    ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)
    try:
        plan = plan_next_session(
            ev_df, grid_df, cfg, ev_id,
            max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
            reserve_soc=reserve_soc, target_soc=target_soc, economics=EconomicsParams.load(),
            departure_time=departure_time, current_soc_pct=current_soc_pct, need_soc_pct=need_soc_pct,
        )
    except UnknownVehicleError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InsufficientHistoryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        explanation = explain_next_session_plan(plan)
    except CopilotNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"plan": plan, "explanation": explanation}
