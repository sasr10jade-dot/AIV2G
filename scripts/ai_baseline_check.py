"""예측 모델(LightGBM)을 단순 규칙(지난주·어제 같은 시간, 24시간 평균)과 같은 홀드아웃에서 비교한다 — AI 역할 팩트체크 근거.

합성 데이터(EV 30대·21주, 시작일 2026-05-31)를 메모리에서 만들어 쓰며 data/ 를 건드리지 않는다.
실행: python scripts/ai_baseline_check.py
"""
import json
import sys
from pathlib import Path

import numpy as np

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.model.demand_forecast import TEST_HOURS, train_forecast_model
from src.model.features import build_features
from src.pipeline.store import build_city_hourly

cfg = SimConfig(start_date="2026-05-31")
rng = np.random.default_rng(cfg.random_seed)
ev_df = generate_ev_telemetry(cfg, rng)
grid_df = generate_grid_signal(cfg, rng)
city = build_city_hourly(ev_df, grid_df, cfg)
out = {}
for target in ("ev_charging_demand_kw", "ev_available_v2g_kwh"):
    r = train_forecast_model(city, target)
    feat, _ = build_features(city, target)
    test = feat.iloc[-TEST_HOURS:]
    y = test[target].to_numpy()
    base = {}
    for name, col in (("전주 같은 시간(lag168)", f"{target}_lag168"), ("어제 같은 시간(lag24)", f"{target}_lag24"),
                      ("직전 24시간 평균", f"{target}_roll24_mean")):
        p = test[col].to_numpy()
        base[name] = round(float(np.abs(p - y).mean()), 3)
    out[target] = {"lightgbm_mae": r.mae, "lightgbm_wape": r.wape, "rmse": r.rmse, "mean_actual": round(float(y.mean()), 3),
                   "baseline_mae": base, "n_train": r.n_train, "n_test": r.n_test, "n_features": len(r.feature_cols)}
print(json.dumps(out, ensure_ascii=False, indent=1))
