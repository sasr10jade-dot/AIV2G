"""실증 데이터 소스 추상화 — 업로드되었거나 생성된 raw 데이터가 있으면 그것을 쓰고,
없으면 SimConfig로 즉석에서 합성 데모 시나리오를 만든다. Serving/Decision 레이어
(forecast_service, optimize/dashboard 라우터)가 데이터 출처를 신경 쓰지 않도록
이 모듈 하나로 감싼다 — 실증 연계 시에는 여기만 교체하면 된다.

실증 데이터는 한 번에 3종(EV/충전기/계통)이 동시에 갖춰지지 않는다 — 사업 소개자료
(Report/*_사업내용_260915.pdf)의 협력요청 구조상 EV 실차 OBD가 제일 먼저 오고,
충전사업자·공공데이터 연계는 별도 협의가 끝나야 온다. 그래서 `has_raw_data()`는
EV 데이터만 있어도 "실증 모드"로 보고, 아직 없는 충전기/계통 데이터는 EV 실측
규모(대수)에 맞춘 합성 데이터로 보완한다 — 부분 실증 상태를 있는 그대로 반영한다.
"""
from dataclasses import replace

import numpy as np
import pandas as pd

from src.datagen.charger import generate_charger_sessions
from src.datagen.config import SimConfig
from src.datagen.ev import generate_ev_telemetry
from src.datagen.grid import generate_grid_signal
from src.paths import RAW_DIR

EV_CSV = RAW_DIR / "ev_telemetry.csv"
CHARGER_CSV = RAW_DIR / "charger_sessions.csv"
GRID_CSV = RAW_DIR / "grid_signal.csv"
SOURCE_MARKER = RAW_DIR / ".source"


def has_raw_data() -> bool:
    """EV 실측 데이터만 있어도 True — 충전기/계통은 아직 없으면 합성으로 보완되므로
    "실증 모드 진입"의 최소 조건은 EV 데이터다."""
    return EV_CSV.exists()


def current_mode() -> str:
    """'uploaded'(실증 데이터) | 'generated'(데모로 생성됨) | 'demo'(RAW_DIR 비어있어 즉석 생성)."""
    if not has_raw_data():
        return "demo"
    if SOURCE_MARKER.exists():
        return SOURCE_MARKER.read_text(encoding="utf-8").strip()
    return "generated"


def mark_source(kind: str) -> None:
    SOURCE_MARKER.write_text(kind, encoding="utf-8")


def clear_raw_data() -> None:
    for path in (EV_CSV, CHARGER_CSV, GRID_CSV, SOURCE_MARKER):
        path.unlink(missing_ok=True)


# 합성 시나리오 한 번을 만드는 데 6~9초가 걸리고 여러 화면(이용자 앱·예측·추천·대시보드)이 같은 값을 쓰므로, 같은 설정·같은 원본
# 파일이면 결과를 잠시 재사용한다. 호출 측이 열을 더하거나 바꿔도 서로 영향이 없도록 항상 복사본을 돌려준다.
import threading  # noqa: E402
import time  # noqa: E402

_SCENARIO_TTL_S = 600
_SCENARIO_MAX = 3
_scenario_cache: dict = {}
_scenario_lock = threading.Lock()


def _raw_fingerprint() -> tuple:
    return tuple(p.stat().st_mtime if p.exists() else None for p in (EV_CSV, CHARGER_CSV, GRID_CSV))


def clear_scenario_cache() -> None:
    with _scenario_lock:
        _scenario_cache.clear()


def load_scenario(cfg: SimConfig | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, bool]:
    """같은 설정·같은 원본 파일이면 캐시한 결과의 복사본을 돌려준다(아래 `_load_scenario_uncached` 참고)."""
    cfg = cfg or SimConfig()
    key = (cfg, _raw_fingerprint(), str(RAW_DIR))
    with _scenario_lock:   # 동시에 같은 시나리오를 여러 번 만들지 않게 한 번에 하나만 계산한다
        hit = _scenario_cache.get(key)
        if hit is None or time.monotonic() - hit[0] > _SCENARIO_TTL_S:
            _scenario_cache[key] = hit = (time.monotonic(), _load_scenario_uncached(cfg))
            while len(_scenario_cache) > _SCENARIO_MAX:
                _scenario_cache.pop(min(_scenario_cache, key=lambda k: _scenario_cache[k][0]))
        ev, ch, gr, real = hit[1]
    return ev.copy(), ch.copy(), gr.copy(), real


def _load_scenario_uncached(cfg: SimConfig) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, bool]:
    """(ev_df, charger_df, grid_df, is_real) 반환.

    RAW_DIR에 데이터가 있으면(업로드됐든 `/ingest/generate`로 생성됐든) 그대로 읽어
    쓰고, 없으면 cfg 기반 합성 데모 시나리오를 만든다. is_real은 업로드된 실측
    데이터인지를 나타낸다(대시보드/API 응답에서 "데모/실증" 구분에 사용).

    EV 데이터는 있는데 충전기·계통 데이터가 아직 없으면(협력 파트너 연계 전 단계),
    EV 실측 대수에 맞춰 합성한 충전기·계통 데이터로 보완한다 — 완전 합성으로
    되돌리지 않는다(이미 확보한 실측 EV 데이터를 버리는 건 잘못된 폴백이다).
    """
    if has_raw_data():
        ev_df = pd.read_csv(EV_CSV, parse_dates=["timestamp"])
        rng = np.random.default_rng(cfg.random_seed)
        synth_cfg = replace(cfg, n_ev=max(ev_df["ev_id"].nunique(), 1))

        charger_df = (
            pd.read_csv(CHARGER_CSV, parse_dates=["start_ts"]) if CHARGER_CSV.exists()
            else generate_charger_sessions(synth_cfg, rng, ev_df=ev_df)
        )
        grid_df = (
            pd.read_csv(GRID_CSV, parse_dates=["timestamp"]) if GRID_CSV.exists()
            else generate_grid_signal(synth_cfg, rng)
        )
        return ev_df, charger_df, grid_df, current_mode() == "uploaded"

    rng = np.random.default_rng(cfg.random_seed)
    ev_df = generate_ev_telemetry(cfg, rng)
    charger_df = generate_charger_sessions(cfg, rng, ev_df=ev_df)
    grid_df = generate_grid_signal(cfg, rng)
    return ev_df, charger_df, grid_df, False
