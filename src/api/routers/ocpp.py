"""OCPP 충전기 통신 API — 실시간 충전기 에뮬레이터 제어 + 상태 조회 (WS4/기술개발전략 Control 레이어).

OCPP 1.6과 2.0.1을 모두 지원한다. V2G(방전) 신호는 2.0.1의 Energy.Active.Export.Register
measurand로만 표현 가능하므로, V2G 사이클을 보려면 protocol="2.0.1"로 에뮬레이터를 실행한다.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field, field_validator

from src.auth.deps import require_admin
from src.datagen.config import SimConfig
from src.ocpp import central_system, emulator
from src.optimizer.poc_compare import run_v2g_optimization
from src.pipeline.source import load_scenario

router = APIRouter(prefix="/ocpp", tags=["ocpp"])


class EmulatorStartRequest(BaseModel):
    n_chargers: int = Field(5, ge=1, le=300, description="새로 접속시킬 가상 충전기 대수 (PRD 목표 200기 기준 여유를 둠)")
    max_charge_kw: float = Field(7.0, gt=0, le=50, description="충전기당 최대 충전 출력(kW)")
    protocol: str = Field(
        "1.6", description="'1.6'(충전만) 또는 '2.0.1'(충전 후 V2G 방전까지 시뮬레이션)"
    )

    @field_validator("protocol")
    @classmethod
    def _check_protocol(cls, v: str) -> str:
        if v not in ("1.6", "2.0.1"):
            raise ValueError("protocol은 '1.6' 또는 '2.0.1'이어야 합니다")
        return v


@router.websocket("/{charge_point_id}")
async def ocpp_endpoint(websocket: WebSocket, charge_point_id: str, protocol: str = Query("1.6")) -> None:
    """OCPP 충전기가 접속하는 WebSocket 엔드포인트 (Central System 본체).

    실제 충전기·SteVe·에뮬레이터가 이 경로로 OCPP-J 프레임을 주고받는다. WebSocket
    엔드포인트라 OpenAPI(Swagger) 스키마에는 자동으로 노출되지 않는다 — 연결 방법은
    `ws://<host>/ocpp/<charge_point_id>?protocol=1.6|2.0.1`.
    """
    await central_system.handle_connection(websocket, charge_point_id, protocol=protocol)


@router.post("/emulator/start", summary="가상 충전기 접속 시작", dependencies=[Depends(require_admin)])
async def start_emulator(req: EmulatorStartRequest, request: Request) -> dict:
    """가상 충전기 n대를 실제 OCPP WebSocket으로 이 서버에 접속시켜 부팅→충전(→2.0.1이면 V2G 방전)
    →종료 사이클을 반복시킨다. 이미 접속된 충전기에 추가로 더 붙는다(누적).

    에뮬레이터는 항상 이 서버 자신에게 루프백으로 접속해야 하므로, 공인 도메인(터널 등)이 아니라
    ASGI가 실제로 바인딩한 host:port(`request.scope["server"]`)를 그대로 써서 접속 대상을 만든다 —
    서버를 어느 포트로 띄우든(기본 OCPP_WS_URL의 8000 가정과 달라도) 항상 올바르게 자기 자신을 찾는다.
    """
    host, port = request.scope.get("server") or ("127.0.0.1", 8000)
    ws_base = f"ws://{host}:{port}/ocpp"
    started = emulator.start_fleet(req.n_chargers, req.max_charge_kw, protocol=req.protocol, ws_base=ws_base)
    return {"started": started, "running": emulator.running_ids()}


@router.post("/emulator/stop", summary="가상 충전기 전체 정지", dependencies=[Depends(require_admin)])
async def stop_emulator() -> dict:
    """접속된 가상 충전기를 전부 접속 해제하고 상태를 초기화한다."""
    stopped = emulator.stop_fleet()
    central_system.clear()
    return {"stopped": stopped}


@router.get("/status", summary="접속된 충전기 실시간 상태 조회", dependencies=[Depends(require_admin)])
def ocpp_status() -> dict:
    """충전기별 프로토콜·상태·순간 전력(kW, 음수=V2G 방전)·누적 충전/방전 전력량을 반환한다.
    대시보드가 2~3초 간격으로 폴링한다."""
    return {"charge_points": central_system.snapshot(), "emulator_running": emulator.running_ids()}


@router.post(
    "/dispatch-ai-schedule",
    summary="CEMS(AI 최적화) 스케줄을 CSMS를 거쳐 실제 접속된 충전기에 명령으로 전달",
    dependencies=[Depends(require_admin)],
)
def dispatch_ai_schedule(
    max_charge_kw: float = Query(7.0, gt=0, le=50),
    max_discharge_kw: float = Query(7.0, ge=0, le=50),
    reserve_soc: float = Query(0.20, ge=0, le=0.5),
    target_soc: float = Query(0.90, ge=0.5, le=0.99),
) -> dict:
    """`decision.py`의 LP가 계산한 세션별 충/방전 스케줄을, 지금 접속돼 있는 가상 충전기들에
    실제로 명령한다(`central_system.set_command`) — 지금까지는 이 스케줄이 대시보드 차트·
    EV Bot 설명용으로만 쓰이고 에뮬레이터 동작과는 무관했는데(CEMS와 CSMS가 분리된 섬),
    이 엔드포인트가 그 둘을 잇는 실제 폐루프다. 충전기 수와 세션 수가 다르면 순서대로
    1:1 매핑하고 남는 쪽은 그냥 둔다(데모 규모 매칭용 — 실제로는 차량-충전기 실물 매핑이 있어야 함).
    """
    cfg = SimConfig()
    ev_df, _charger_df, grid_df, _is_real = load_scenario(cfg)
    result = run_v2g_optimization(
        ev_df, grid_df, cfg,
        max_charge_kw=max_charge_kw, max_discharge_kw=max_discharge_kw,
        reserve_soc=reserve_soc, target_soc=target_soc,
    )
    schedule = result["schedule"]
    sessions = result["sessions"]
    session_ev_map = result["session_ev_map"]

    connected_cp_ids = sorted(central_system.snapshot().keys())
    if not connected_cp_ids:
        raise HTTPException(status_code=404, detail="접속된 충전기가 없습니다 — 먼저 에뮬레이터를 시작하세요")
    non_empty = [sid for sid in schedule if schedule[sid]]
    if not non_empty:
        raise HTTPException(status_code=404, detail="배차할 V2G 세션이 없습니다(시나리오에 연결 구간이 없음)")
    # 방전 활동이 가장 뚜렷한 세션부터 배차 — 접속된 충전기 수만큼만 필요하므로 "가장 보여줄
    # 만한" 세션을 우선한다(전체 수천 개 세션 중 앞쪽 몇 개만 알파벳순으로 뽑으면 대부분
    # 충전만 하고 끝나는 재미없는 세션일 확률이 높다).
    session_ids = sorted(
        non_empty, key=lambda sid: sum(d for _c, d in schedule[sid].values()), reverse=True
    )[: len(connected_cp_ids)]

    dispatched = []
    for cp_id, session_id in zip(connected_cp_ids, session_ids):
        # 세션 내에서 가장 뚜렷한 행동(충전 또는 방전 절대량이 가장 큰 시점)을 대표값으로 뽑는다
        # — 단순히 세션의 첫 시각을 쓰면 "이 시간엔 대기만"인 0/0 시점이 걸리기 쉬워 데모 효과가 없다.
        best_t = max(schedule[session_id], key=lambda t: abs(schedule[session_id][t][0] - schedule[session_id][t][1]))
        charge_kw, discharge_kw = schedule[session_id][best_t]
        central_system.set_command(cp_id, charge_kw=round(charge_kw, 2), discharge_kw=round(discharge_kw, 2))
        dispatched.append({
            "cp_id": cp_id, "session_id": session_id, "ev_id": session_ev_map.get(session_id),
            "charge_kw": round(charge_kw, 2), "discharge_kw": round(discharge_kw, 2),
        })

    return {
        "dispatched": dispatched,
        "n_connected_chargers": len(connected_cp_ids),
        "n_sessions_available": len(session_ids),
        "note": "각 충전기는 다음 세션 시작(Authorize) 시점부터 이 명령을 반영합니다(즉시 반영 아님).",
    }


@router.post("/clear-ai-schedule", summary="CEMS 명령 전체 해제(다시 자율 시뮬레이션으로 복귀)", dependencies=[Depends(require_admin)])
def clear_ai_schedule() -> dict:
    central_system.clear_commands()
    return {"status": "ok"}
