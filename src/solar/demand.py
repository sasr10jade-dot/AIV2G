"""사이트 충전 수요 생성 — 도착 시각·체류 시간·필요 에너지를 가진 충전 세션과 충전기 배정.

수요 패턴은 임의 가정이다(사이트 설정 `demand`로 규모만 조절). 도착 시간대별로 체류 시간이 다르다:
- 오전 도착(출근형) 체류 6~9시간, 점심 도착 2~4시간, 오후 도착 2~5시간, 저녁 도착(야간 체류) 8~13시간.
충전기가 모두 사용 중일 때 도착한 차는 충전하지 못한 것으로 세고(unserved), 필요 에너지가 체류 시간 동안
충전기 출력으로 채울 수 있는 양보다 크면 가능한 양으로 줄이고 모자란 양을 unmet_kwh로 기록한다.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from src.solar.site import SolarSite

# (이름, 가중치, 체류 시간 범위(시간, 양끝 포함)) — 평일/주말 도착 시간대별
_WEEKDAY = (
    ("morning", range(7, 11), 0.30, (6, 9)),
    ("midday", range(11, 15), 0.20, (2, 4)),
    ("afternoon", range(15, 18), 0.12, (2, 5)),
    ("evening", range(18, 23), 0.38, (8, 13)),
)
_WEEKEND = (
    ("morning", range(9, 12), 0.35, (2, 6)),
    ("afternoon", range(12, 18), 0.40, (2, 6)),
    ("evening", range(18, 22), 0.25, (8, 12)),
)
MAX_DWELL_H = 13


@dataclass(frozen=True)
class Session:
    session_id: int
    arrival: int        # 시뮬레이션 시작(0시)부터의 시간 인덱스
    departure: int      # 이 시각에 출발(배타적)
    need_kwh: float     # 실제로 채울 수 있는 에너지(체류·출력 한도 반영)
    charger: int
    requested_kwh: float
    bucket: str

    @property
    def window(self) -> range:
        return range(self.arrival, self.departure)


def horizon_hours(days: int) -> int:
    """도착일 수 + 마지막 날 늦은 도착 세션이 끝날 때까지의 여유 시간."""
    return days * 24 + MAX_DWELL_H + 1


def generate_sessions(site: SolarSite, start_date, days: int, seed: int = 0) -> tuple[list[Session], int, float]:
    """(서비스한 세션 목록, 충전기가 없어 놓친 세션 수, 체류 시간 부족으로 못 채운 kWh)."""
    if days < 1:
        raise ValueError("days는 1 이상이어야 합니다")
    start = pd.Timestamp(start_date).normalize()
    dem = site.demand
    candidates: list[tuple[int, int, float, str]] = []     # (arrival, dwell, requested_kwh, bucket)
    for d in range(days):
        day = start + pd.Timedelta(days=d)
        rng = np.random.default_rng([seed, day.toordinal(), 3])
        weekend = day.dayofweek >= 5
        table = _WEEKEND if weekend else _WEEKDAY
        n = int(rng.poisson(dem.sessions_weekend if weekend else dem.sessions_weekday))
        weights = np.array([b[2] for b in table])
        for _ in range(n):
            name, hours, _w, (dmin, dmax) = table[int(rng.choice(len(table), p=weights / weights.sum()))]
            hour = int(rng.choice(list(hours)))
            dwell = int(rng.integers(dmin, dmax + 1))
            kwh = float(np.clip(rng.normal(dem.mean_kwh, dem.sd_kwh), dem.min_kwh, dem.max_kwh))
            candidates.append((d * 24 + hour, dwell, kwh, name))
    candidates.sort(key=lambda c: (c[0], c[1]))

    free_at = [0] * site.n_chargers
    sessions: list[Session] = []
    unserved, unmet = 0, 0.0
    for arrival, dwell, requested, bucket in candidates:
        slot = next((i for i, t in enumerate(free_at) if t <= arrival), None)
        if slot is None:
            unserved += 1
            continue
        departure = arrival + dwell
        feasible = min(requested, dwell * site.charger_kw)
        unmet += requested - feasible
        free_at[slot] = departure
        sessions.append(Session(len(sessions), arrival, departure, feasible, slot, requested, bucket))
    return sessions, unserved, unmet
