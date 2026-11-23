"""VPP·이익공유 실증 API — 출력제어 해소 에뮬레이터(C01), 재원별 편익(S01), 가상 전력 거래·모의 포인트 정산(E03),
V2G 이동형 ESS 집중 실증 그룹(D03).

계산은 대시보드 기본 시나리오(SimConfig 기본값 = 실증 규모)와 같은 데이터·같은 LP 최적화 결과를 쓴다.
결과는 데이터가 바뀌거나 10분이 지나면 다시 계산하고, 무거운 계산은 한 번에 하나만 돈다(서버 응답 불능 방지).
"""
from __future__ import annotations

import io
import sqlite3
import threading
import time
from datetime import datetime, timezone

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.auth.deps import client_ip, require_admin, require_owner
from src.privacy import audit

router = APIRouter(tags=["vpp"])

_TTL_SEC = 600
_cache: dict = {}
_lock = threading.Lock()
MAX_KW = 7.0


def _affiliation_of(ev_df):
    """등록부 소속(관용/민간)을 우선 쓰고, 없으면 차량 유형으로 추정한다(추정임을 표기)."""
    from src.registry import service as registry

    types = ev_df.groupby("ev_id")["ev_type"].first().to_dict() if "ev_type" in ev_df.columns else {}

    def f(ev_id: str):
        aff = registry.affiliation_for_ev(ev_id)
        if aff:
            return aff, "등록부"
        return ("public" if "업무" in str(types.get(ev_id, "")) else "private"), "추정(차량 유형)"
    return f


def build_bundle(cfg) -> dict:
    """시나리오 데이터 + LP 최적화 + 출력제어 에뮬레이션 + 재원별 편익을 한 번에 계산한다(캐시 없음)."""
    from src.optimizer import curtailment, vpp_revenue
    from src.optimizer.economics import EconomicsParams
    from src.optimizer.poc_compare import aggregate_comparison, compute_fleet_revenue_comparison, run_v2g_optimization
    from src.pipeline import source

    ev_df, charger_df, grid_df, is_real = source.load_scenario(cfg)
    eco = EconomicsParams.load()
    run = run_v2g_optimization(ev_df, grid_df, cfg, max_charge_kw=MAX_KW, max_discharge_kw=MAX_KW,
                               reserve_soc=0.20, target_soc=0.90, economics=eco)
    comparison = aggregate_comparison(ev_df, run["schedule"])
    revenue = compute_fleet_revenue_comparison(comparison, grid_df, eco)
    params = curtailment.load_params()
    prov = source.provenance()
    curt = curtailment.emulate(grid_df, ev_df, comparison, MAX_KW, MAX_KW, eco.charge_efficiency, eco.discharge_efficiency,
                               params=params, provenance=prov)
    sources = vpp_revenue.compute_revenue_sources(revenue, curt, params)
    return {"cfg": cfg, "ev_df": ev_df, "charger_df": charger_df, "grid_df": grid_df,
            "is_real": is_real, "run": run, "comparison": comparison, "revenue": revenue, "params": params,
            "provenance": prov, "curtailment": curt, "revenue_sources": sources}


def bundle() -> dict:
    """기본 시나리오(실증 규모) 계산 결과 — 데이터가 바뀌거나 10분이 지나면 다시 계산, 계산은 한 번에 하나만."""
    from src.datagen.config import SimConfig
    from src.pipeline import source

    key = source._raw_fingerprint()
    with _lock:
        hit = _cache.get("v")
        if hit and hit["key"] == key and time.time() - hit["at"] < _TTL_SEC:
            return hit
        out = {**build_bundle(SimConfig()), "key": key, "at": time.time()}
        _cache["v"] = out
        return out


# ------------------------------------------------------------------ C01 출력제어 해소
@router.get("/vpp/curtailment", summary="신재생 출력제어 해소 시나리오 — 과잉발전 패턴·전후 출력제어량 비교(에뮬레이터)",
            dependencies=[Depends(require_admin)])
def get_curtailment() -> dict:
    from src.optimizer.curtailment import public_view
    b = bundle()
    return {**public_view(b["curtailment"]), "provenance": b["provenance"]}


# ------------------------------------------------------------------ S01 재원별 편익
@router.get("/vpp/revenue-sources", summary="재원별 편익 — 요금 차익·피크 저감·출력제어 완화(산식 기반, 고정 비율 아님)",
            dependencies=[Depends(require_admin)])
def get_revenue_sources() -> dict:
    b = bundle()
    return b["revenue_sources"]


# ------------------------------------------------------------------ E03 가상 전력 거래·모의 정산(리워드)
def _consent_of(ev_id: str):
    """등록 차량이면 방전(V2G) 참여 동의 여부, 등록부에 없는 데모 차량이면 None."""
    from src.privacy import consent
    from src.registry import service as registry
    if registry.profile_for_ev(ev_id) is None:
        return None
    return consent.has_consent(ev_id, "v2g_discharge")


def _trades(b: dict):
    from src.settlement import ledger
    return ledger.compute_trades(b["run"], b["ev_df"], b["curtailment"], b["revenue_sources"], b["params"],
                                 _affiliation_of(b["ev_df"]), _consent_of)


def _data_source(b: dict) -> str:
    return "measured_telemetry" if b["provenance"].get("ev_telemetry") == "measured" else "synthetic"


def _settle_err(fn, *args):
    from src.settlement import ledger
    try:
        return fn(*args)
    except ledger.SettlementError as exc:
        raise HTTPException(status_code=404 if "찾을 수 없" in str(exc) else 409, detail=str(exc)) from exc


class RunIn(BaseModel):
    period_from: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="정산 기간 시작(YYYY-MM-DD). 비우면 실적 첫날")
    period_to: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="정산 기간 끝. 비우면 실적 마지막 날")
    month: str | None = Field(None, pattern=r"^\d{4}-\d{2}$", description="월 1회 정산(YYYY-MM) — 주면 그 달 1일~말일로 산정")


@router.post("/vpp/settlement/runs", summary="정산 회차 산정 — 기간 실적을 재원별로 배분해 포인트 계산(확정 전, 원장 미적립)")
def create_settlement_run(body: RunIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    b = bundle()
    trades = _trades(b)
    if trades.empty:
        raise HTTPException(status_code=409, detail="정산할 실적이 없습니다")
    pf = body.period_from or trades["trade_date"].min()
    pt = body.period_to or trades["trade_date"].max()
    if body.month:
        last_day = (pd.Period(body.month, "M").end_time).strftime("%Y-%m-%d")
        pf, pt = f"{body.month}-01", last_day
    run = _settle_err(ledger.create_run, trades, pf, pt, b["params"], user["username"], "inferred", _data_source(b))
    audit.log(user["username"], "settlement_run_create", target=str(run["run_id"]), detail={"from": pf, "to": pt, "ip": client_ip(request)})
    return run


@router.get("/kpi/dataset", summary="실증 데이터셋(결합 레코드) 현황 — 이벤트 종류별 건수·결합 성공률·연차별 성과 건수·확보 계획(평가의견 4·14)",
            dependencies=[Depends(require_admin)])
def dataset_status(sample: int = Query(0, ge=0, le=50, description="결합 레코드 예시 행 수(가명 처리)")) -> dict:
    import json

    from src.datagen.config import SimConfig
    from src.pipeline import dataset, source
    from src.privacy.pseudonym import pseudo_id
    ev_df, charger_df, grid_df, is_real = source.load_scenario(SimConfig())
    ds = dataset.build_dataset(ev_df, charger_df, grid_df)
    out = {"is_real_data": is_real, "provenance": source.provenance(), **dataset.summarize(ds)}
    if sample and not ds.empty:
        cols = [c for c in ("source", "kind", "ev_id", "start_ts", "end_ts", "soc_start_pct", "soc_end_pct", "energy_kwh",
                            "charger_id", "site_id", *dataset.CONTEXT_COLS, "context_ok", "counted", "project_year") if c in ds.columns]
        rows = ds[cols].head(sample).copy()
        rows["ev_id"] = rows["ev_id"].map(lambda v: pseudo_id("ev", str(v)) if isinstance(v, str) and v else None)
        out["sample"] = json.loads(rows.to_json(orient="records", date_format="iso", force_ascii=False))
    return out


@router.get("/kpi/management", summary="성과·관리 지표판 — 필수성과 5·관리지표 7·경제적 편익 4·원천별 데이터·레퍼런스·보상 단계(평가의견 5·6·8·10·11)",
            dependencies=[Depends(require_admin)])
def management_board(evaluate: bool = Query(False, description="true면 예측 정확도를 지금 계산(수십 초), 아니면 캐시된 평가만 사용")) -> dict:
    from src import management
    from src.api.routers import models as models_router
    from src.appadmin import survey
    from src.optimizer import ess_group
    from src.settlement import ledger
    b = bundle()
    accuracy = None
    hit = models_router._eval_cache.get("v")
    evals = models_router.plan_target_evaluations()[0] if evaluate else (hit[2] if hit else None)
    if evals:
        ev = evals.get("ev_charging_demand_kw") or next(iter(evals.values()))
        accuracy = {"accuracy_pct": ev["test"].get("accuracy_pct"), "basis": "검증(시험) 구간 · " + ev["target_col"]}
    members = [m["ev_id"] for m in _members()]
    cands = ess_group.analyze_candidates(b["ev_df"], b["charger_df"], _affiliation_of(b["ev_df"]))
    agg = ess_group.aggregate_capacity(b["ev_df"], members or [c["ev_id"] for c in ess_group.recommend(cands)])
    trades = _trades(b)
    incentives = ledger.simulate_incentives(trades, b["params"]) if not trades.empty else None
    from src.automation.model_registry import ModelRegistry
    try:
        model_ready = ModelRegistry("ev_charging_demand_kw").active_version() is not None
    except Exception:
        model_ready = None
    aff = _affiliation_of(b["ev_df"])
    public_vehicles = sum(1 for e in b["ev_df"]["ev_id"].unique() if aff(e)[0] == "public")
    return management.build_board(b, accuracy=accuracy, settlement=ledger.summary(b["params"]), surveys=survey.list_all(),
                                  ess_aggregate=agg, incentives=incentives, model_ready=model_ready, public_vehicles=public_vehicles)


@router.get("/vpp/settlement/months", summary="월별 정산 상태 — 월 1회 모의 정산(미정산·산정·확정)", dependencies=[Depends(require_admin)])
def settlement_months() -> dict:
    from src.settlement import ledger
    return {"months": ledger.month_status(_trades(bundle()))}


@router.get("/vpp/settlement/institution-report", summary="관용차량 기관 단위 모의 정산 리포트(월·차량별, CSV 가능)",
            dependencies=[Depends(require_admin)])
def institution_report(month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"), fmt: str = Query("json", pattern="^(json|csv)$")):
    from src.settlement import ledger
    rep = ledger.institution_report(month)
    if fmt == "json":
        return rep
    rows = pd.DataFrame(rep["by_month"]).assign(구분="월별") if rep["by_month"] else pd.DataFrame()
    veh = pd.DataFrame(rep["by_vehicle"]).assign(구분="차량별") if rep["by_vehicle"] else pd.DataFrame()
    buf = io.StringIO()
    pd.concat([rows, veh], ignore_index=True).to_csv(buf, index=False)
    buf.write("\n# " + rep["note"] + "\n")
    return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=institution_settlement_report.csv"})


@router.get("/vpp/settlement/runs", summary="정산 회차 목록(산정·확정·취소)", dependencies=[Depends(require_admin)])
def list_settlement_runs() -> dict:
    from src.settlement import ledger
    return {"runs": ledger.list_runs()}


@router.get("/vpp/settlement/runs/{run_id}", summary="정산 회차 상세 — 차량·일별 재원 배분·포인트", dependencies=[Depends(require_admin)])
def get_settlement_run(run_id: int) -> dict:
    from src.settlement import ledger
    return {**_settle_err(ledger.get_run, run_id), "trades": ledger.run_trades(run_id)}


class DecideIn(BaseModel):
    note: str = Field("", max_length=200)


@router.post("/vpp/settlement/runs/{run_id}/confirm", summary="정산 회차 확정 — 포인트를 원장에 적립(참여자 잔액 반영)")
def confirm_settlement_run(run_id: int, body: DecideIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    run = _settle_err(ledger.confirm_run, run_id, user["username"], body.note)
    audit.log(user["username"], "settlement_run_confirm", target=str(run_id), detail={"ip": client_ip(request)})
    return run


@router.post("/vpp/settlement/runs/{run_id}/cancel", summary="정산 회차 취소(산정 상태만) — 사유 필수, 같은 기간을 다시 산정할 수 있다")
def cancel_settlement_run(run_id: int, body: DecideIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    run = _settle_err(ledger.cancel_run, run_id, user["username"], body.note)
    audit.log(user["username"], "settlement_run_cancel", target=str(run_id), detail={"note": body.note, "ip": client_ip(request)})
    return run


@router.post("/vpp/settlement/run", summary="정산 한 번에 실행(전체 기간 산정 + 즉시 확정) — 간편 실행용, 중복 정산 없음")
def run_settlement(request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    b = bundle()
    trades = _trades(b)
    result = ledger.settle(trades, "inferred", _data_source(b), b["params"], user["username"])
    audit.log(user["username"], "vpp_settlement_run", detail={**result, "ip": client_ip(request)})
    return {**result, "trades_computed": len(trades), "summary": ledger.summary(b["params"])}


@router.get("/vpp/settlement/summary", summary="모의 정산 집계 — 관용/민간별 참여·포인트(확정 기준), 정산 가능범위, 경제성 검증",
            dependencies=[Depends(require_admin)])
def settlement_summary() -> dict:
    from src.optimizer.curtailment import load_params
    from src.settlement import ledger
    return ledger.summary(load_params())


@router.get("/vpp/settlement/preview", summary="정산 미리보기(원장에 쓰지 않음) — 재원 배분·경제성 검증·인센티브 유형 비교(포인트·할인·수익배분·마일리지)",
            dependencies=[Depends(require_admin)])
def settlement_preview(discount_rate_pct: float = Query(10.0, ge=0, le=100),
                       revenue_share_pct: float | None = Query(None, ge=0, le=100)) -> dict:
    from src.settlement import ledger
    b = bundle()
    trades = _trades(b)
    return {
        "rules": b["params"]["points"],
        "peak_hours": b["params"]["curtailment"]["peak_hours"],
        "revenue_sources": b["revenue_sources"],
        "trades": trades.head(200).to_dict("records"),
        "n_trades": len(trades),
        "economics": ledger.economics_check(trades, b["params"]),
        "incentives": ledger.simulate_incentives(trades, b["params"], discount_rate_pct, revenue_share_pct),
        "label": "모의(추론 방전량·가정 단가)",
    }


class AdjustIn(BaseModel):
    ev_id: str = Field(..., min_length=3, max_length=40)
    points: int = Field(..., ge=-1_000_000, le=1_000_000)
    reason: str = Field(..., min_length=2, max_length=200)


@router.post("/vpp/settlement/adjust", summary="포인트 조정(가산·차감) — 사유 필수, 원장에 남는다")
def adjust_points(body: AdjustIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    out = _settle_err(ledger.adjust, body.ev_id, body.points, body.reason, user["username"])
    audit.log(user["username"], "points_adjust", target=body.ev_id, detail={"points": body.points, "reason": body.reason, "ip": client_ip(request)})
    return out


@router.get("/vpp/settlement/inquiries", summary="참여자 정산 문의 목록", dependencies=[Depends(require_admin)])
def list_settlement_inquiries(status: str | None = Query(None, pattern="^(open|resolved)$")) -> dict:
    from src.settlement import ledger
    return {"inquiries": ledger.list_inquiries(status)}


class ResolveIn(BaseModel):
    answer: str = Field(..., min_length=2, max_length=500)
    adjust_points: int = Field(0, ge=-1_000_000, le=1_000_000, description="답변과 함께 조정할 포인트(0이면 조정 없음)")


@router.post("/vpp/settlement/inquiries/{inquiry_id}/resolve", summary="정산 문의 답변(필요 시 포인트 조정)")
def resolve_settlement_inquiry(inquiry_id: int, body: ResolveIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    out = _settle_err(ledger.resolve_inquiry, inquiry_id, body.answer, user["username"], body.adjust_points)
    audit.log(user["username"], "settlement_inquiry_resolve", target=str(inquiry_id), detail={"adjust": body.adjust_points, "ip": client_ip(request)})
    return out


@router.get("/owner/points", summary="내 모의 포인트 — 확정 잔액·확정 대기·재원별 내역·조정·문의·등급")
def owner_points(user: dict = Depends(require_owner)) -> dict:
    from src.settlement import ledger, missions
    out = ledger.owner_statement(user["owned_ev_id"])
    out["level"] = missions.level_of(out["balance_points"])
    return out


class InquiryIn(BaseModel):
    message: str = Field(..., min_length=2, max_length=500)
    trade_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post("/owner/points/inquiries", summary="정산 문의 남기기(본인 차량) — 관리자가 답변·조정")
def owner_points_inquiry(body: InquiryIn, user: dict = Depends(require_owner)) -> dict:
    from src.settlement import ledger
    try:
        return ledger.create_inquiry(user["owned_ev_id"], body.message, body.trade_date)
    except ledger.SettlementError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ------------------------------------------------------------------ D03 이동형 ESS 집중 실증 그룹
def _members() -> list[dict]:
    from src.pipeline import store
    con = store.get_connection()
    try:
        rows = con.execute("SELECT ev_id, group_type, score, reason, selected_at, selected_by FROM ess_group_members ORDER BY score DESC").fetchall()
    finally:
        con.close()
    return [dict(zip(("ev_id", "group_type", "score", "reason", "selected_at", "selected_by"), r)) for r in rows]


@router.get("/vpp/ess-group", summary="V2G 이동형 ESS 집중 실증 그룹 — 후보 지표(규칙성·장기주차·완속 체류·심야충전 후 주간대기)와 지정 현황",
            dependencies=[Depends(require_admin)])
def get_ess_group() -> dict:
    from src.optimizer import ess_group
    b = bundle()
    cands = ess_group.analyze_candidates(b["ev_df"], b["charger_df"], _affiliation_of(b["ev_df"]))
    members = _members()
    recommended = [c["ev_id"] for c in ess_group.recommend(cands)]
    basis = [m["ev_id"] for m in members] or recommended
    return {
        "group_size_target": [ess_group.GROUP_MIN, ess_group.GROUP_MAX],
        "group_size_default": ess_group.GROUP_DEFAULT,
        "members": members,
        "recommended": recommended,
        "candidates": cands,
        "criteria": "점수 = 규칙성 30 + 장기 주차 30 + 완속 체류 20 + 심야충전·주간대기 20 (0~100)",
        "criteria_rule": f"거점 주차 {ess_group.CRITERIA_PARK_HOURS}시간 이상·주 {ess_group.CRITERIA_PER_WEEK}회 이상 연결 차량 우선, "
                         f"기본 {ess_group.GROUP_DEFAULT}대 (평가의견 13)",
        "aggregate": {**ess_group.aggregate_capacity(b["ev_df"], basis), "basis": "지정 그룹" if members else "추천 차량"},
        "label": "실측" if b["provenance"].get("ev_telemetry") == "measured" else "모의(합성 데이터)",
    }


class EssGroupIn(BaseModel):
    ev_ids: list[str] = Field(..., min_length=5, max_length=10, description="그룹으로 지정할 차량 5~10대")
    reason: str = Field("", max_length=200)


@router.put("/vpp/ess-group", summary="ESS 집중 실증 그룹 지정(5~10대) — 기존 지정을 바꾼다")
def set_ess_group(body: EssGroupIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.optimizer import ess_group
    from src.pipeline import store
    b = bundle()
    cands = {c["ev_id"]: c for c in ess_group.analyze_candidates(b["ev_df"], b["charger_df"], _affiliation_of(b["ev_df"]))}
    ids = list(dict.fromkeys(body.ev_ids))
    unknown = [e for e in ids if e not in cands]
    if unknown:
        raise HTTPException(status_code=422, detail=f"데이터에 없는 차량입니다: {', '.join(unknown[:5])}")
    if not ess_group.GROUP_MIN <= len(ids) <= ess_group.GROUP_MAX:
        raise HTTPException(status_code=422, detail="그룹은 5~10대로 지정합니다")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = store.get_connection()
    try:
        con.execute("DELETE FROM ess_group_members")
        con.executemany(
            "INSERT INTO ess_group_members (ev_id, group_type, score, reason, selected_at, selected_by) VALUES (?,?,?,?,?,?)",
            [(e, cands[e]["group_type"], cands[e]["score"], body.reason.strip() or None, now, user["username"]) for e in ids])
        con.commit()
    except sqlite3.Error:
        con.rollback()
        raise
    finally:
        con.close()
    audit.log(user["username"], "ess_group_set", detail={"ev_ids": ids, "ip": client_ip(request)})
    return {"members": _members()}


@router.get("/vpp/ess-group/dataset.csv", summary="전기차 배터리 가상 ESS 자원화 전용 데이터셋(CSV) — 지정 그룹 차량의 시간별 ESS 파생 지표",
            dependencies=[Depends(require_admin)])
def ess_dataset() -> StreamingResponse:
    from src.optimizer import ess_group
    members = [m["ev_id"] for m in _members()]
    if not members:
        raise HTTPException(status_code=409, detail="먼저 ESS 집중 실증 그룹(5~10대)을 지정하세요")
    b = bundle()
    df = ess_group.build_dataset(b["ev_df"], members)
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=ess_group_dataset.csv"})


# ------------------------------------------------------------------ 서포터즈 미션(가상 리워드)
def _mission_err(fn, *args):
    from src.settlement import missions
    try:
        return fn(*args)
    except missions.MissionError as exc:
        raise HTTPException(status_code=409 if "이미" in str(exc) or "아직" in str(exc) else 422, detail=str(exc)) from exc


@router.get("/owner/missions", summary="내 미션 — 진행도·완료·보상 수령 여부, 등급")
def owner_missions(user: dict = Depends(require_owner)) -> dict:
    from src.settlement import ledger, missions
    bal = ledger.balance(user["owned_ev_id"])
    return {"missions": missions.status_for(user["owned_ev_id"], user["username"]), "level": missions.level_of(bal),
            "note": "미션 보상은 모의 포인트입니다(실제 금전 지급 아님)."}


@router.post("/owner/missions/{mission_id}/claim", summary="완료한 미션 보상 받기 — 기간당 1회, 포인트 원장 적립")
def owner_mission_claim(mission_id: str, user: dict = Depends(require_owner)) -> dict:
    from src.settlement import missions
    out = _mission_err(missions.claim, user["owned_ev_id"], mission_id, user["username"])
    audit.log(user["username"], "mission_claim", target=mission_id, detail={"points": out["points"], "period": out["period_key"]})
    return out


@router.get("/vpp/missions", summary="미션 관리 — 미션별 설정·완료(보상) 현황, 등급 기준", dependencies=[Depends(require_admin)])
def admin_missions() -> dict:
    from src.settlement import missions
    return missions.admin_overview()


class MissionSettingIn(BaseModel):
    enabled: bool = True
    points: int = Field(..., ge=0, le=100_000)


@router.put("/vpp/missions/{mission_id}", summary="미션 설정 변경 — 사용 여부·보상 포인트")
def admin_update_mission(mission_id: str, body: MissionSettingIn, request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import missions
    out = _mission_err(missions.update_setting, mission_id, body.enabled, body.points, user["username"])
    audit.log(user["username"], "mission_update", target=mission_id, detail={"enabled": body.enabled, "points": body.points, "ip": client_ip(request)})
    return out
