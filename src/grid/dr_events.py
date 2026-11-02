"""한전 계통 수요반응(DR, Demand Response) 이벤트 — CEMS 핵심 기능 중 우리 시스템에
빠져 있던 부분(Ai-V2g-Cems-Agent.html 목업의 "한전 DR 이벤트 수신, 가격 신호 연동").

실제 한전 DR 신호를 받는 공개 API를 찾지 못해(이전 분석: e-policy.or.kr은 지역×시간
조합 데이터가 없고, data.go.kr의 KPX API들도 DR 이벤트 자체는 제공하지 않음)
관리자가 수동으로 DR 이벤트를 등록하는 스텁으로 구현한다 — OCPP/OBD 에뮬레이터와
같은 이유(실제 신호원 없이도 그 아래 계층 로직을 검증).

**연동 방식**: DR 이벤트를 LP에 새로운 제약식으로 추가하지 않고, 목업이 스스로
표현한 대로 "가격 신호 연동"으로 구현한다 — DR 활성 시간대의 `price_by_time`에
프리미엄을 곱해 LP가 이미 갖고 있는 가격 기반 valley-filling/방전 유인 로직이
자연히 그 시간대에 더 강하게 방전하도록 만든다. 새 제약 타입을 만들지 않아도 되고,
"가격이 오르면 방전한다"는 기존 로직과 일관된다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd

from src.timeutil import to_kst_naive

DR_EVENTS: dict[str, dict] = {}

# 심각도별 가격 프리미엄 배수 — 높을수록 LP가 그 시간대에 더 강하게 방전하도록 유인
SEVERITY_MULTIPLIER = {"low": 1.5, "medium": 2.5, "high": 4.0}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_dr_event(start_ts: pd.Timestamp, end_ts: pd.Timestamp, severity: str = "medium", reason: str = "") -> str:
    if severity not in SEVERITY_MULTIPLIER:
        raise ValueError(f"알 수 없는 심각도입니다: {severity} (선택지: {', '.join(SEVERITY_MULTIPLIER)})")
    # 화면은 UTC(`...Z`)로 보내고 시나리오는 한국시간 naive 관례라, 저장 전에 맞춘다(안 맞추면 가격 신호 계산이 TypeError)
    start_ts, end_ts = to_kst_naive(start_ts), to_kst_naive(end_ts)
    if end_ts <= start_ts:
        raise ValueError("end_ts는 start_ts보다 이후여야 합니다")

    event_id = f"DR-{uuid.uuid4().hex[:8]}"
    DR_EVENTS[event_id] = {
        "event_id": event_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "severity": severity,
        "reason": reason,
        "created_at": _now(),
    }
    return event_id


def list_dr_events() -> list[dict]:
    return [
        {**e, "start_ts": e["start_ts"].isoformat(), "end_ts": e["end_ts"].isoformat()}
        for e in sorted(DR_EVENTS.values(), key=lambda e: e["start_ts"])
    ]


def delete_dr_event(event_id: str) -> None:
    if event_id not in DR_EVENTS:
        raise ValueError(f"존재하지 않는 DR 이벤트입니다: {event_id}")
    del DR_EVENTS[event_id]


def clear_dr_events() -> None:
    DR_EVENTS.clear()


def apply_dr_premium_to_price(price_by_time: dict[pd.Timestamp, float]) -> dict[pd.Timestamp, float]:
    """DR 이벤트가 활성인 시간대의 가격에 심각도별 프리미엄을 곱해 반환한다(원본은
    변경하지 않음). 여러 이벤트가 겹치면 가장 높은 배수를 적용한다."""
    if not DR_EVENTS:
        return price_by_time

    adjusted = dict(price_by_time)
    for t, price in price_by_time.items():
        multiplier = 1.0
        for event in DR_EVENTS.values():
            if event["start_ts"] <= t < event["end_ts"]:
                multiplier = max(multiplier, SEVERITY_MULTIPLIER[event["severity"]])
        if multiplier > 1.0:
            adjusted[t] = price * multiplier
    return adjusted
