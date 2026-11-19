"""운영 재학습 사이클 배치 — 요구사항 Y2B01·Y2B02(운영상황 변화를 반영하는 의사결정 갱신 구조).

지금 쌓인 데이터(실측이 있으면 실측, 없으면 데모 시나리오)로 예측 대상 2종의 드리프트를 재고,
임계치(20%)를 넘으면 재학습 → 기존 대비 2% 이상 나아지면 승격, 아니면 거부(기존 모델 유지)한다.
결과는 data/batch/retrain_history.jsonl 에 한 줄씩 쌓고, 예외는 웹앱 관리 > 예외 기록에 남는다.

사용: python scripts/run_retrain_cycle.py
주기 실행
  - macOS(서버 PC): launchd — docs/인수인계_개발자_필독_가이드.txt '재학습 배치' 참고(매주 월 05:00)
  - Windows: 작업 스케줄러에 이 명령을 주 1회 등록
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.automation.model_registry import ModelRegistry  # noqa: E402
from src.automation.scheduler import run_cycle_on_data  # noqa: E402
from src.datagen.config import SimConfig  # noqa: E402
from src.paths import DATA_DIR  # noqa: E402
from src.pipeline import source  # noqa: E402
from src.pipeline.store import build_city_hourly  # noqa: E402

TARGETS = ("ev_charging_demand_kw", "ev_available_v2g_kwh")
HISTORY = DATA_DIR / "batch" / "retrain_history.jsonl"


def main() -> int:
    cfg = SimConfig()
    ev_df, _ch, grid_df, _real = source.load_scenario(cfg)
    city_hourly = build_city_hourly(ev_df, grid_df, cfg)
    data_source = "measured" if source.provenance().get("ev_telemetry") == "measured" else "synthetic"
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = []
    for target in TARGETS:
        try:
            results.append(run_cycle_on_data(ModelRegistry(target), city_hourly, target, data_source))
        except Exception as exc:   # 한 대상이 실패해도 다른 대상은 계속
            results.append({"target_col": target, "action": "ERROR", "detail": str(exc)[:300]})
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"started_at": started, "data_source": data_source, "results": results}, ensure_ascii=False) + "\n")
    for r in results:
        print(f"{r['target_col']}: {r['action']} (drift {r.get('drift_pct', '-')}%, 후보 MAE {r.get('candidate_mae', '-')})")
    return 1 if any(r["action"] == "ERROR" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
