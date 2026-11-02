"""차량별 입출차(플러그인/아웃) 패턴 예측 — 새 ML 모델을 학습·재학습하지 않는다.

출퇴근처럼 매주 반복되는 생활 패턴은 `ModelRegistry`의 드리프트 감지·재학습 사이클을
붙일 이유가 없다 — 그 차량의 과거 (요일, 시간대)별 연결(plugged-in) 빈도를 그대로
다음 구간에 투영하는 것만으로 충분히 설명 가능하고, 매번 정확히 재계산되므로 오히려
더 투명하다.

최소 `MIN_WEEKS_FOR_PATTERN`주 이력이 있어야 요일별 패턴이 의미 있다. 실제 차량은
그만큼 쌓이기 전까지 `src/datagen/obd_emulator.py`(`POST /ingest/obd-emulator/run`)로
여러 주 분량을 합성해 두 함수를 검증할 수 있다 — 같은 `ev_telemetry` 스키마를 쓰므로
실측 데이터로 교체돼도 이 모듈은 바뀌지 않는다.
"""
from __future__ import annotations

import pandas as pd

MIN_WEEKS_FOR_PATTERN = 2.0


class UnknownVehicleError(ValueError):
    """해당 ev_id의 이력이 전혀 없을 때."""


class InsufficientHistoryError(ValueError):
    """이력은 있지만 요일별 패턴을 낼 만큼(최소 2주) 충분하지 않을 때."""


def _hourly_connection_table(hist: pd.DataFrame) -> pd.DataFrame:
    hist = hist.copy()
    hist["dow"] = hist["timestamp"].dt.dayofweek
    hist["hour"] = hist["timestamp"].dt.hour
    return hist.groupby(["dow", "hour"])["connected"].mean().reset_index()


def _average_departure_soc(hist_sorted: pd.DataFrame) -> float | None:
    """"정지(연결/대기) → 주행" 전환 직전 시간의 soc_pct 평균 — 그 차량이 보통 어느
    SoC로 집을 나서는지에 대한 이력 기반 근사치. 사용자가 명시적으로 설정한 목표
    SoC가 아니라 "과거에 실제로 그랬던" 값이라는 한계가 있다."""
    driving = hist_sorted["driving"].fillna(False).astype(bool)
    departures = []
    for i in range(1, len(hist_sorted)):
        if driving.iloc[i] and not driving.iloc[i - 1]:
            soc_val = hist_sorted["soc_pct"].iloc[i - 1]
            if pd.notna(soc_val):
                departures.append(float(soc_val))
    if not departures:
        return None
    return round(sum(departures) / len(departures), 1)


def forecast_plugin_schedule(ev_df: pd.DataFrame, ev_id: str, horizon_hours: int = 48) -> dict:
    """`ev_id`의 과거 (요일, 시간대)별 연결 빈도를 다음 `horizon_hours`에 투영하고,
    이력상 평균 출차 시각·SoC를 함께 반환한다."""
    hist = ev_df[ev_df["ev_id"] == ev_id].copy()
    if hist.empty:
        raise UnknownVehicleError(f"차량 이력이 없습니다: {ev_id}")

    hist = hist.sort_values("timestamp").reset_index(drop=True)
    n_weeks = (hist["timestamp"].max() - hist["timestamp"].min()).total_seconds() / (3600 * 24 * 7)
    if n_weeks < MIN_WEEKS_FOR_PATTERN:
        raise InsufficientHistoryError(
            f"입출차 패턴 분석에는 최소 {MIN_WEEKS_FOR_PATTERN:.0f}주 이력이 필요합니다"
            f"(현재 {n_weeks:.1f}주) — OBD 에뮬레이터로 이력을 더 합성하거나 실측 데이터가 더 쌓인 뒤 다시 시도하세요."
        )

    table = _hourly_connection_table(hist)
    lookup = {(int(r.dow), int(r.hour)): float(r.connected) for r in table.itertuples()}

    start = hist["timestamp"].max() + pd.Timedelta(hours=1)
    timestamps = pd.date_range(start, periods=horizon_hours, freq="h")
    probs = [lookup.get((ts.dayofweek, ts.hour), 0.0) for ts in timestamps]

    expected_departure = None
    for i in range(1, len(probs)):
        if probs[i - 1] >= 0.5 and probs[i] < 0.5:
            expected_departure = timestamps[i]
            break

    return {
        "ev_id": ev_id,
        "n_weeks_history": round(n_weeks, 1),
        "timestamps": [ts.isoformat() for ts in timestamps],
        "connection_probability": [round(p, 2) for p in probs],
        "expected_next_departure": expected_departure.isoformat() if expected_departure is not None else None,
        "expected_departure_soc_pct": _average_departure_soc(hist),
    }
