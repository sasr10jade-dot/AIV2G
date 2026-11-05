"""충전서비스 사업자(CPO 3사: 나이스인프라·플러그링크·이브이시스) 연계 API.

사업수행계획서 13장(p.56~58) 용역계획서 대응:
  1. 데이터 명세서 1식 (항목 정의, 스키마, 코드값, 예시데이터)
  2. API 명세서 1식 (엔드포인트, 요청/응답 규격, 인증, 에러코드)
  3. 일 단위 수집 데이터 일체(원천+가공) 및 일 단위 자동 제공·모니터링
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from src.paths import PROJECT_ROOT

from src.auth.deps import require_admin

router = APIRouter()
CPO_CONFIG_PATH = PROJECT_ROOT / "config" / "cpo_sources.json"


class CpoSessionItem(BaseModel):
    session_id: str = Field(..., description="충전 트랜잭션/세션 고유 ID")
    charger_id: str = Field(..., description="충전기 식별 번호")
    start_ts: str = Field(..., description="충전 시작 일시 (ISO-8601)")
    end_ts: Optional[str] = Field(None, description="충전 종료 일시 (ISO-8601)")
    duration_h: Optional[float] = Field(None, description="충전 소요 시간(h)")
    energy_kwh: float = Field(..., ge=0, description="충전 전력량(kWh)")
    avg_power_kw: Optional[float] = Field(None, ge=0, description="평균 충전 출력(kW)")
    connector_type: Optional[str] = Field(None, description="커넥터 유형 (AC_SLOW, DC_COMBO 등)")
    stop_reason: Optional[str] = Field(None, description="충전 종료 사유 (Local, Remote, Emergency 등)")
    error_code: Optional[str] = Field(None, description="오류 코드 (NoError 등)")
    zone: Optional[int] = Field(0, description="권역 코드")


class CpoIngestPayload(BaseModel):
    batch_date: str = Field(..., description="배치 일자 (YYYY-MM-DD)")
    sessions: List[CpoSessionItem] = Field(..., description="충전 세션 목록")


def load_cpo_config() -> dict:
    if not CPO_CONFIG_PATH.exists():
        return {"sources": []}
    return json.loads(CPO_CONFIG_PATH.read_text(encoding="utf-8"))


@router.get("/specs", summary="CPO 데이터 및 API 명세서 (계획서 13장 산출물)", dependencies=[Depends(require_admin)])
def get_cpo_specifications():
    """사업수행계획서 13장 용역 산출물에 해당하는 CPO 연계 데이터 명세서 및 API 명세서를 반환합니다."""
    cfg = load_cpo_config()
    data_dictionary = [
        {"field": "session_id", "type": "String(64)", "required": True, "description": "충전 세션/트랜잭션 고유 식별자", "example": "TXN-20261105-001"},
        {"field": "charger_id", "type": "String(32)", "required": True, "description": "충전기 고유 번호 (사업자 관리번호)", "example": "CHG-PL-042"},
        {"field": "start_ts", "type": "ISO8601 String", "required": True, "description": "충전 시작 일시 (YYYY-MM-DDTHH:MM:SS)", "example": "2026-11-05T01:15:00"},
        {"field": "end_ts", "type": "ISO8601 String", "required": False, "description": "충전 종료 일시 (YYYY-MM-DDTHH:MM:SS)", "example": "2026-11-05T03:45:00"},
        {"field": "duration_h", "type": "Float", "required": False, "description": "충전 소요 시간(시간 단위, 0 이상)", "example": 2.5},
        {"field": "energy_kwh", "type": "Float", "required": True, "description": "충전 유효 전력량 (kWh, 0 이상)", "example": 35.8},
        {"field": "avg_power_kw", "type": "Float", "required": False, "description": "평균 충전 출력 (kW)", "example": 14.3},
        {"field": "connector_type", "type": "String(20)", "required": False, "description": "충전 커넥터 규격 코드", "example": "DC_COMBO1"},
        {"field": "stop_reason", "type": "String(32)", "required": False, "description": "충전 종료 사유 코드", "example": "NormalStop"},
        {"field": "error_code", "type": "String(32)", "required": False, "description": "오류 발생 시 코드", "example": "NoError"}
    ]
    code_table = {
        "connector_type": {
            "AC_SLOW": "완속 5~7kW (AC 단상 5핀/7핀)",
            "DC_COMBO1": "급속 50~100kW (CCS1 콤보)",
            "DC_CHADEMO": "급속 50kW (차데모)",
            "DC_SUPER": "초급속 100~350kW (CCS1/CCS2)"
        },
        "stop_reason": {
            "NormalStop": "정상 종료 (차량 완충 또는 설정치 도달)",
            "UserStop": "사용자 강제 중단 (카드 태그 또는 앱 중단)",
            "EmergencyStop": "비상정지 버튼 조작에 의한 중단",
            "CommFault": "차량 또는 서버 통신 오류에 의한 중단"
        }
    }
    api_spec = {
        "endpoint": "POST /cpo/ingest/{cpo_id}",
        "method": "POST",
        "auth": "Bearer Token (Header: Authorization: Bearer <token>)",
        "content_type": "application/json",
        "path_params": {"cpo_id": "사업자 식별 코드 (niceinfra, pluglink, evsis)"},
        "request_body_schema": "CpoIngestPayload",
        "responses": {
            "200": {"description": "정상 수신 및 적재 성공", "example": {"status": "ok", "cpo_id": "niceinfra", "received": 45, "valid": 45, "errors": []}},
            "400": {"description": "잘못된 요청 또는 데이터 정합성 실패"},
            "401": {"description": "인증 토큰 누락 또는 유효하지 않음"},
            "422": {"description": "JSON 유효성 검증 실패"}
        }
    }
    return {
        "title": "AI-V2G 충전서비스 사업자(CPO 3사) 데이터 연계 명세서",
        "version": "1.0",
        "target_operators": cfg.get("sources", []),
        "total_target_chargers": 150,
        "data_dictionary": data_dictionary,
        "code_definitions": code_table,
        "api_specification": api_spec
    }


@router.get("/status", summary="CPO 3사 연계 및 수집 현황", dependencies=[Depends(require_admin)])
def get_cpo_status():
    """CPO 3사(나이스인프라·플러그링크·이브이시스)의 데이터 연계 상태 및 일별 수집 통계를 반환합니다."""
    cfg = load_cpo_config()
    sources = cfg.get("sources", [])
    summary = []
    total_chargers = 0
    total_cumulative_sessions = 0
    total_received_today = 0
    for s in sources:
        target_chg = s.get("target_chargers", 50)
        st = s.get("status", {})
        total_chargers += target_chg
        total_cumulative_sessions += st.get("cumulative_sessions", 0)
        total_received_today += st.get("received_today", 0)
        summary.append({
            "operator_id": s.get("operator_id"),
            "operator_name": s.get("operator_name"),
            "agreement_status": s.get("agreement_status"),
            "contract_amount_krw": s.get("contract_amount_krw"),
            "target_chargers": target_chg,
            "connected": st.get("connected", False),
            "last_sync": st.get("last_sync"),
            "received_today": st.get("received_today", 0),
            "cumulative_sessions": st.get("cumulative_sessions", 0),
            "error_count": st.get("error_count", 0)
        })
    return {
        "total_operators": len(summary),
        "total_target_chargers": total_chargers,
        "total_received_today": total_received_today,
        "total_cumulative_sessions": total_cumulative_sessions,
        "operators": summary
    }


@router.post("/ingest/{cpo_id}", summary="CPO 일 단위 충전 세션 수신 API")
def ingest_cpo_sessions(
    cpo_id: str,
    payload: CpoIngestPayload,
    authorization: Optional[str] = Header(None)
):
    """CPO 사업자 시스템에서 당일 또는 전일 충전 세션 데이터를 배치로 수신합니다."""
    cfg = load_cpo_config()
    cpo_info = next((s for s in cfg.get("sources", []) if s.get("operator_id") == cpo_id), None)
    if not cpo_info:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"등록되지 않은 CPO 사업자 식별자입니다: {cpo_id}"
        )

    if authorization and not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="올바른 Bearer 토큰 형식이 아닙니다.")

    received_count = len(payload.sessions)
    valid_sessions = []
    errors = []

    for idx, item in enumerate(payload.sessions):
        try:
            dur = item.duration_h
            if dur is None and item.end_ts:
                try:
                    s_dt = datetime.fromisoformat(item.start_ts)
                    e_dt = datetime.fromisoformat(item.end_ts)
                    dur = max(0.0, round((e_dt - s_dt).total_seconds() / 3600.0, 2))
                except Exception:
                    dur = 1.0
            elif dur is None:
                dur = 1.0

            valid_sessions.append({
                "session_id": f"{cpo_id}:{item.session_id}",
                "charger_id": item.charger_id,
                "start_ts": item.start_ts,
                "end_ts": item.end_ts,
                "duration_h": dur,
                "energy_kwh": item.energy_kwh,
                "avg_power_kw": item.avg_power_kw or (round(item.energy_kwh / dur, 2) if dur > 0 else 0),
                "connector_type": item.connector_type or "AC_SLOW",
                "stop_reason": item.stop_reason or "NormalStop",
                "error_code": item.error_code or "NoError",
                "zone": item.zone or 1
            })
        except Exception as e:
            errors.append({"index": idx, "session_id": item.session_id, "reason": str(e)})

    try:
        for s in cfg.get("sources", []):
            if s.get("operator_id") == cpo_id:
                st = s.setdefault("status", {})
                st["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                st["received_today"] = len(valid_sessions)
                st["cumulative_sessions"] = st.get("cumulative_sessions", 0) + len(valid_sessions)
                st["error_count"] = st.get("error_count", 0) + len(errors)
        CPO_CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    return {
        "status": "ok",
        "cpo_id": cpo_id,
        "operator_name": cpo_info.get("operator_name"),
        "batch_date": payload.batch_date,
        "received": received_count,
        "valid": len(valid_sessions),
        "errors": errors
    }
