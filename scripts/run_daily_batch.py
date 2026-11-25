"""일 배치 — 데이터 결합(C4) → 예측 갱신(C1) → 아크릴 오차 계산(C3) → 차량별 충전 계획·앱 추천(C2).

사용: python scripts/run_daily_batch.py            (추천 기록 없이 계산만: --no-issue)
주기 실행
  - macOS(서버 PC): scripts/register_daily_launchd.sh — 매일 04:30
  - Windows: 작업 스케줄러에 이 명령을 매일 1회 등록
결과: data/batch/daily/<날짜>/, data/batch/daily_history.jsonl, 실패는 웹앱 관리 > 예외 기록
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from src.automation.daily_batch import run_daily  # noqa: E402


def main() -> int:
    out = run_daily(issue="--no-issue" not in sys.argv[1:])
    print(f"[{out['day']}] 데이터 출처: {out.get('data_source')}")
    for s in out["steps"]:
        print(f"  {s['step']:<12} {'OK ' if s['ok'] else '실패'} {s['seconds']}s {s.get('error', '')}")
    if out.get("dataset"):
        d = out["dataset"]
        print(f"  데이터셋: {d['counted']}건 / 이벤트 {d['events']} (결합 성공률 {d['join_rate_pct']}%)")
    for t, f in (out.get("forecast") or {}).items():
        print(f"  예측 {t}: {f['model_source']} v{f['model_version']} — 48시간 합 {f['total']}, 최대 {f['peak_at']}")
    if out.get("recommend"):
        r = out["recommend"]
        print(f"  추천: 대상 {r['vehicles']}대, 발행 {r['issued']}, 실패 {r['failed']} (SOC 가정 {r['soc_assumed']}대)")
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
