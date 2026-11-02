"""보유기간 경과 개인정보 파기 — 반려된 가입 신청, 법정 보관 기간이 지난 접속기록.

기본은 확인만 하는 dry-run이고, 실제로 지우려면 `dry_run=False`(스크립트의 `--apply`)를 명시한다.
기간은 config/retention.json에서 읽는다(법무 확인 전 값은 verified=false).

- 반려된 가입 신청: 반려(검토) 시각으로부터 rejected_registration_days가 지나면 삭제한다(번호판 암호문 포함).
  승인된 신청과 대기 중인 신청은 건드리지 않는다. 회원이 살아 있는 한 보유 목적이 계속된다.
- 접속기록(감사로그): access_log_days가 지난 앞쪽 기록을 지우고 체크포인트를 남겨 체인 검증이 이어지게 한다.
- 탈퇴 회원의 개인정보는 탈퇴 시점에 이미 삭제된다(`registry.withdrawal`) — 여기서 다룰 것은 그 이후 남는 기록뿐이다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.paths import PROJECT_ROOT
from src.pipeline import store
from src.privacy import audit

RETENTION_CONFIG = PROJECT_ROOT / "config" / "retention.json"


@dataclass(frozen=True)
class RetentionPolicy:
    access_log_days: int = 730
    rejected_registration_days: int = 30
    verified: bool = False


def load_policy(path: Path | None = None) -> RetentionPolicy:
    p = Path(path or RETENTION_CONFIG)
    if not p.exists():
        return RetentionPolicy()
    cfg = json.loads(p.read_text(encoding="utf-8"))
    policy = RetentionPolicy(
        access_log_days=int(cfg.get("access_log_days", 730)),
        rejected_registration_days=int(cfg.get("rejected_registration_days", 30)),
        verified=bool(cfg.get("verified", False)),
    )
    if policy.access_log_days < 1 or policy.rejected_registration_days < 1:
        raise ValueError("보유기간은 1일 이상이어야 합니다")
    return policy


def _cutoff(now: datetime, days: int) -> str:
    return (now - timedelta(days=days)).isoformat()


def purge_rejected_registrations(days: int, now: datetime | None = None, dry_run: bool = True) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = _cutoff(now, days)
    con = store.get_connection()
    try:
        where = "status='rejected' AND COALESCE(reviewed_at, created_at) < ?"
        n = con.execute(f"SELECT COUNT(*) FROM vehicle_registrations WHERE {where}", (cutoff,)).fetchone()[0]
        if n and not dry_run:
            con.execute(f"DELETE FROM vehicle_registrations WHERE {where}", (cutoff,))
            con.commit()
        return int(n)
    finally:
        con.close()


def purge_access_logs(days: int, now: datetime | None = None, dry_run: bool = True) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = _cutoff(now, days)
    return audit.count_before(cutoff) if dry_run else audit.purge_before(cutoff)


def run(dry_run: bool = True, now: datetime | None = None, policy: RetentionPolicy | None = None) -> dict:
    """모든 파기 대상을 처리하고 건수를 돌려준다. 결과 자체도 감사로그에 남긴다(실제로 지운 경우만)."""
    policy = policy or load_policy()
    now = now or datetime.now(timezone.utc)
    result = {
        "dry_run": dry_run,
        "policy_verified": policy.verified,
        "rejected_registrations": purge_rejected_registrations(policy.rejected_registration_days, now, dry_run),
        "access_logs": purge_access_logs(policy.access_log_days, now, dry_run),
    }
    if not dry_run and (result["rejected_registrations"] or result["access_logs"]):
        audit.log("system", "retention_purge", detail={
            "rejected_registrations": result["rejected_registrations"], "access_logs": result["access_logs"],
            "access_log_days": policy.access_log_days, "rejected_registration_days": policy.rejected_registration_days,
        })
    return result
