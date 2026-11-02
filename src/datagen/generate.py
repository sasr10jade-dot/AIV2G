"""합성 실증 데이터 생성 CLI: EV 텔레메트리 · 충전기 세션 · 계통 신호를 raw CSV로 저장."""
import numpy as np

from src.paths import RAW_DIR
from .config import SimConfig
from .ev import generate_ev_telemetry
from .charger import generate_charger_sessions
from .grid import generate_grid_signal


def run(cfg: SimConfig | None = None) -> dict:
    cfg = cfg or SimConfig()
    rng = np.random.default_rng(cfg.random_seed)

    ev_df = generate_ev_telemetry(cfg, rng)
    charger_df = generate_charger_sessions(cfg, rng, ev_df=ev_df)
    grid_df = generate_grid_signal(cfg, rng)

    ev_df.to_csv(RAW_DIR / "ev_telemetry.csv", index=False)
    charger_df.to_csv(RAW_DIR / "charger_sessions.csv", index=False)
    grid_df.to_csv(RAW_DIR / "grid_signal.csv", index=False)
    (RAW_DIR / ".source").write_text("generated", encoding="utf-8")

    return {
        "ev_telemetry_rows": len(ev_df),
        "charger_sessions_rows": len(charger_df),
        "grid_signal_rows": len(grid_df),
    }


if __name__ == "__main__":
    stats = run()
    for k, v in stats.items():
        print(f"{k}: {v:,}")
