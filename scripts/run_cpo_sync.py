"""충전사업자 세션 동기화 CLI — config/cpo_sources.json의 활성 항목을 curated.db에 반영한다.

  python scripts/run_cpo_sync.py            # 전체 활성 사업자
  python scripts/run_cpo_sync.py --since 2026-09-01

한 사업자가 실패하면 종료코드 1(나머지 사업자는 정상 반영된다).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cpo.adapters import load_adapters  # noqa: E402
from src.cpo.sync import sync_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="이 날짜 이후 세션만 (YYYY-MM-DD)")
    args = ap.parse_args()
    adapters = load_adapters()
    if not adapters:
        print("활성화된 사업자가 없습니다 (config/cpo_sources.json의 enabled 확인)")
        return 0
    summary = sync_all(adapters, since=args.since)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
