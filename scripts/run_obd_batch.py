"""OBD 일일 배치 실행 CLI — 작업 스케줄러/cron에서 매일 새벽(예: 03:30, 05:00 재확인)에 호출한다.

    python scripts/run_obd_batch.py                  # OBD_BATCH_MODE(기본 shadow)로 실행
    python scripts/run_obd_batch.py --mode live      # 운영 반영
    python scripts/run_obd_batch.py --fetch          # TOAST 버킷에서 어제치(+--fetch-days)를 받아 처리
    python scripts/run_obd_batch.py --promote        # shadow로 검증해 둔 staging 결과를 운영에 반영
    python scripts/run_obd_batch.py --replay OBDData --limit 20   # 과거 CSV 리플레이(원본 이동 없음, shadow)

종료 코드: 0 정상, 1 격리·오류 파일 있음(알림 대상), 2 인자 오류.
"""
import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import batch_ingest as b  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=[b.MODE_SHADOW, b.MODE_LIVE])
    ap.add_argument("--base", help="배치 작업 폴더(기본: data/batch)")
    ap.add_argument("--min-age", type=float, default=b.DEFAULT_MIN_AGE_SECONDS, help="전송 완료로 보는 최소 경과 초")
    ap.add_argument("--promote", action="store_true", help="shadow staging 결과를 운영에 반영")
    ap.add_argument("--evs", help="--promote 대상 ev_id(쉼표 구분, 생략 시 전체)")
    ap.add_argument("--replay", help="과거 OBD CSV 폴더를 shadow로 리플레이(원본 이동 없음)")
    ap.add_argument("--limit", type=int, default=0, help="--replay 시 처리할 최대 파일 수")
    ap.add_argument("--purge-days", type=int, default=0, help="N일 지난 원본 삭제(0이면 안 함)")
    ap.add_argument("--fetch", action="store_true", help="TOAST Object Storage에서 등록 차량의 CSV를 incoming으로 내려받은 뒤 처리")
    ap.add_argument("--fetch-days", type=int, default=2, help="--fetch 시 어제부터 거슬러 올라갈 일수(늦은 도착분 보완, 기본 2)")
    args = ap.parse_args()

    paths = b.make_paths(Path(args.base) if args.base else None)

    if args.promote:
        evs = {x.strip() for x in args.evs.split(",")} if args.evs else None
        print(json.dumps(b.promote_staged(paths, evs), ensure_ascii=False, indent=2))
        return 0

    if args.replay:
        registry = b.load_registry()
        files = sorted(Path(args.replay).glob("*.csv"))
        if args.limit:
            files = files[: args.limit]
        counts: dict[str, int] = {}
        for f in files:
            r = b.process_file(f, paths, registry, b.MODE_SHADOW, move=False)
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            print(f"{f.name}: {r['status']} {r.get('reason', '')} valid={r.get('hours_valid')}/{r.get('hours_total')}")
        print(json.dumps(counts, ensure_ascii=False))
        return 1 if counts.get("quarantined") or counts.get("error") else 0

    fetch_report = None
    if args.fetch:
        from src.pipeline import toast_fetch as tf

        registry = b.load_registry()
        plates = {p for p, v in registry.items() if v.get("active", True)}
        storage = tf.ToastStorage(tf.ToastConfig.from_env())
        today = b.default_target_date()
        fetch_report = [tf.fetch_day(storage, today - timedelta(days=i), paths.incoming, paths.state_db, plates)
                        for i in range(args.fetch_days)]

    report = b.run_daily_batch(paths, mode=args.mode, min_age_seconds=args.min_age)
    if args.purge_days:
        report["purged_raw"] = b.purge_old_raw(paths, args.purge_days)
    if fetch_report is not None:
        print(json.dumps({"fetch": [{k: (len(v) if isinstance(v, list) else v) for k, v in d.items()} for d in fetch_report]},
                         ensure_ascii=False))
        if any(d["errors"] for d in fetch_report):
            print("다운로드 오류:", json.dumps([e for d in fetch_report for e in d["errors"]], ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("mode", "files_processed", "files_pending_transfer", "counts", "arrival")},
                     ensure_ascii=False, indent=2))
    bad = report["counts"].get("quarantined", 0) + report["counts"].get("error", 0)
    fetch_failed = bool(fetch_report and any(d["errors"] for d in fetch_report))
    return 1 if bad or fetch_failed or report["arrival"]["missing_ev_ids"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
