"""합성 실증 데이터 생성 공통 설정.

기본값은 사업수행계획서 v1.0 실증 규모(EV 30대, 충전기 150기, 21주)를 그대로 반영해,
`SimConfig()`만으로 성과지표 산출식(4장)과 정합하는 데이터를 생성할 수 있게 한다.
"""
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class SimConfig:
    n_ev: int = 30
    weeks: int = 21
    n_chargers: int = 150
    charger_days: int = 147
    n_zones: int = 4
    random_seed: int = 42
    # None이면 __post_init__에서 "서울 시간 기준 지금부터 weeks주 전"으로 자동 계산한다 —
    # 라이브 데모 대시보드의 최신 데이터가 항상 실제 "오늘"에 맞춰 보이도록(예전엔
    # "2026-09-01" 고정이라 관측기간이 길면 실제 오늘보다 몇 달 앞선 날짜가 보였음).
    # 특정 날짜로 재현성 있게 테스트하려면 문자열을 직접 넘기면 이 자동계산을 건너뛴다.
    start_date: str | None = None
    battery_capacity_kwh: float = 64.0
    charger_avg_sessions_per_day: float = 0.2  # 150기 x 147일 x 0.2 = 4,410건

    def __post_init__(self) -> None:
        if self.start_date is None:
            now_kst = pd.Timestamp.now(tz="Asia/Seoul").tz_localize(None)
            computed = (now_kst.normalize() - pd.Timedelta(weeks=self.weeks)).strftime("%Y-%m-%d")
            object.__setattr__(self, "start_date", computed)

    @property
    def n_days(self) -> int:
        return self.weeks * 7

    @property
    def n_hours(self) -> int:
        return self.n_days * 24
