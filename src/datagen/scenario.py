"""합성 시나리오(차량 OBD·거점 충전 세션·계통 신호)를 한 번에 만든다 — load_scenario의 합성 경로와 같은 순서·같은 난수열.

예측모델 학습(재학습 주기·Closed-loop)과 대시보드가 같은 데이터 구성(서포터즈 OBD + 거점 충전 세션)으로 학습 라벨을
만들도록 이 함수 하나를 쓴다(2026-11-23, 결합 데이터셋의 AI 학습 반영).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .charger import generate_charger_sessions
from .config import SimConfig
from .ev import generate_ev_telemetry
from .grid import generate_grid_signal


def generate_scenario(cfg: SimConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    charger_df = generate_charger_sessions(cfg, rng, ev_df=ev_df)
    grid_df = generate_grid_signal(cfg, rng)
    return ev_df, charger_df, grid_df


def synthetic_city_hourly(cfg: SimConfig) -> pd.DataFrame:
    from src.pipeline.store import build_city_hourly

    ev_df, charger_df, grid_df = generate_scenario(cfg)
    return build_city_hourly(ev_df, grid_df, cfg, charger_df=charger_df)
