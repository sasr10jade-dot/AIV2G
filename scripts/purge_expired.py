"""보유기간 경과 개인정보 파기 — 기본은 확인만(dry-run), 실제로 지우려면 --apply.

  python scripts/purge_expired.py            # 지울 건수만 출력
  python scripts/purge_expired.py --apply    # 실제 파기 (먼저 data/ 백업 권장)

운영에서는 하루 1회 정도 예약 실행한다. 기간은 config/retention.json.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.privacy import retention  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="실제로 삭제한다(없으면 dry-run)")
    args = ap.parse_args()
    result = retention.run(dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["policy_verified"]:
        print("※ 보유기간 설정이 법무 확인 전(verified=false)입니다.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
