"""접속기록 정기 점검 보고서 — 개인정보를 다룬 관리자 활동 중 이상 징후를 찾는다.

안전성 확보조치는 접속기록을 정기적으로 확인·점검하도록 요구한다(주기·방법은 내부 관리계획에서 정함).
이 모듈은 그 점검을 쉽게 하는 도구로, 기간 내 활동을 관리자별로 집계하고 아래 징후를 표시한다.

- 하루에 번호판 전체 열람(pii_view)이 임계값을 넘은 관리자 — 대량 열람 의심
- 심야·새벽(한국시간 22시~06시)에 일어난 번호판 열람
- 감사로그 체인 무결성

임계값은 운영하면서 조정한다(기본값은 출발점일 뿐 법정 기준이 아니다).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from src.privacy import audit

KST = timezone(timedelta(hours=9))
BULK_PII_VIEWS_PER_DAY = 20
OFF_HOURS_KST = set(range(22, 24)) | set(range(0, 6))
WATCHED = ("pii_view", "registrations_list", "member_history_view", "registration_approve", "registration_reject")


def report(days: int = 30, now: datetime | None = None, bulk_threshold: int = BULK_PII_VIEWS_PER_DAY) -> dict:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    rows = [e for e in audit.entries(None, None, limit=1_000_000) if e["ts"] >= cutoff and e["action"] in WATCHED]

    per_actor: dict[str, Counter] = defaultdict(Counter)
    pii_per_day: dict[tuple, int] = Counter()
    off_hours = []
    for e in rows:
        per_actor[e["actor"]][e["action"]] += 1
        if e["action"] == "pii_view":
            local = datetime.fromisoformat(e["ts"]).astimezone(KST)
            pii_per_day[(e["actor"], local.date().isoformat())] += 1
            if local.hour in OFF_HOURS_KST:
                off_hours.append({"ts_kst": local.isoformat(timespec="seconds"), "actor": e["actor"], "target": e["target"]})

    bulk = [{"actor": a, "date": d, "pii_views": n} for (a, d), n in sorted(pii_per_day.items()) if n > bulk_threshold]
    flags = []
    if bulk:
        flags.append("bulk_pii_view")
    if off_hours:
        flags.append("off_hours_pii_view")
    chain = audit.verify_chain()
    if not chain["ok"]:
        flags.append("audit_chain_broken")
    return {
        "period_days": days,
        "events": len(rows),
        "per_actor": {a: dict(c) for a, c in sorted(per_actor.items())},
        "bulk_pii_view": bulk,
        "off_hours_pii_view": off_hours,
        "chain": chain,
        "flags": flags,
        "thresholds": {"bulk_pii_views_per_day": bulk_threshold, "off_hours_kst": "22:00~06:00"},
    }
