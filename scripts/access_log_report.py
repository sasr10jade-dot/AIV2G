"""접속기록 정기 점검 보고서 출력 — 개인정보를 다룬 관리자 활동의 이상 징후 확인.

  python scripts/access_log_report.py --days 30

징후가 있으면 종료 코드 1을 돌려줘 예약 실행에서 알림 조건으로 쓸 수 있다.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.privacy import access_review  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="점검 기간(일), 기본 30")
    args = ap.parse_args()
    result = access_review.report(days=args.days)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["flags"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
