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


# ------------------------------------------------------------------ E03 가상 전력 거래·모의 정산
def _trades(b: dict):
    from src.settlement import ledger
    return ledger.compute_trades(b["run"], b["ev_df"], b["curtailment"], b["params"], _affiliation_of(b["ev_df"]))


@router.post("/vpp/settlement/run", summary="가상 전력 거래 정산 실행 — 차량·일별 피크 방전·흡수 실적을 포인트로 적립(중복 정산 없음)")
def run_settlement(request: Request, user: dict = Depends(require_admin)) -> dict:
    from src.settlement import ledger
    b = bundle()
    trades = _trades(b)
    measured = b["provenance"].get("ev_telemetry") == "measured"
    # 1차년도는 물리 방전이 없어 방전량은 최적 스케줄 기반 추론값이다
    result = ledger.settle(trades, generation="inferred", data_source="measured_telemetry" if measured else "synthetic")
    audit.log(user["username"], "vpp_settlement_run", detail={**result, "ip": client_ip(request)})
    return {**result, "trades_computed": len(trades), "summary": ledger.summary()}


@router.get("/vpp/settlement/summary", summary="모의 정산 집계 — 관용/민간별 참여·kWh·포인트, 정산 가능범위", dependencies=[Depends(require_admin)])
def settlement_summary() -> dict:
    from src.settlement import ledger
    return ledger.summary()


@router.get("/vpp/settlement/preview", summary="정산 미리보기(원장에 쓰지 않음) + 인센티브 유형별(포인트·충전요금 할인·수익배분) 비교",
            dependencies=[Depends(require_admin)])
def settlement_preview(discount_rate_pct: float = Query(10.0, ge=0, le=100),
                       revenue_share_pct: float | None = Query(None, ge=0, le=100)) -> dict:
    from src.settlement import ledger
    b = bundle()
    trades = _trades(b)
    return {
        "rules": b["params"]["points"],
        "peak_hours": b["params"]["curtailment"]["peak_hours"],
        "trades": trades.head(200).to_dict("records"),
        "n_trades": len(trades),
        "incentives": ledger.simulate_incentives(trades, b["params"], discount_rate_pct, revenue_share_pct),
        "label": "모의(추론 방전량·가정 단가)",
    }


@router.get("/owner/points", summary="내 모의 포인트 — 잔액·거래 내역(가상 전력 거래 정산)")
def owner_points(user: dict = Depends(require_owner)) -> dict:
    from src.settlement import ledger
    return ledger.owner_statement(user["owned_ev_id"])


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
    return {
        "group_size_target": [ess_group.GROUP_MIN, ess_group.GROUP_MAX],
        "members": members,
        "recommended": [c["ev_id"] for c in ess_group.recommend(cands)],
        "candidates": cands,
        "criteria": "점수 = 규칙성 30 + 장기 주차 30 + 완속 체류 20 + 심야충전·주간대기 20 (0~100)",
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
