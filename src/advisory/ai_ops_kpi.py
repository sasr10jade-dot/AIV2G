"""AI 운영 지표 — 감사로그·동의 이력에서 집계한다 (AI 관리체계 9장 성과평가: 위험통제 준수율·철회율 등).

추천 안전 검증 결과(`owner_guide_issue`)와 EV Bot 호출(`owner_ask`)은 `privacy.audit`에, 추천 응답은 `advisories`에,
방전 참여 동의·철회는 `privacy.consent`에 남아 있다.
순편익·예측 오차는 대시보드 데이터(`/dashboard/data`의 revenue_comparison·forecast_fit)가 이미 제공한다.
"""
from __future__ import annotations

import json
from collections import Counter

from src.advisory import service as advisory
from src.privacy import audit, consent


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def compute() -> dict:
    con = audit._connect()
    try:
        rows = con.execute("SELECT action, detail FROM audit_log WHERE action IN ('owner_guide_issue','owner_ask')").fetchall()
    finally:
        con.close()
    issued = passed = fallback = recommended = participating = asks = 0
    failed: Counter = Counter()
    for action, detail in rows:
        if action == "owner_ask":
            asks += 1
            continue
        d = json.loads(detail) if detail else {}
        issued += 1
        passed += bool(d.get("safety_passed"))
        fallback += bool(d.get("safety_fallback"))
        recommended += bool(d.get("v2g_recommended"))
        participating += bool(d.get("participating"))
        failed.update(d.get("failed_checks") or [])

    counts = advisory.acceptance_stats()["counts"]
    accepted, declined = counts.get("accepted", 0), counts.get("declined", 0)
    cc = consent.connect()
    try:
        granted, withdrawn, active = cc.execute(
            "SELECT COUNT(*), COUNT(withdrawn_at), COUNT(*) - COUNT(withdrawn_at) FROM consents WHERE purpose='v2g_discharge'").fetchone()
    finally:
        cc.close()

    return {
        "recommendations": {
            "issued": issued,
            "safety_pass_rate": _rate(passed, issued),          # 위험통제 준수율 — 검증 6항목을 모두 통과한 비율
            "safety_fallback_count": fallback,                   # 검증 실패로 방전 없는 계획으로 복귀한 건수
            "failed_checks": dict(failed),
            "v2g_recommended_rate": _rate(recommended, issued),
            "participating_rate": _rate(participating, issued),
            "accepted": int(accepted), "declined": int(declined),
            "acceptance_rate": _rate(int(accepted), int(accepted) + int(declined)),   # 추천 수용률 — 응답한 추천 중 '추천대로 할게요' 비율
        },
        "ev_bot": {"calls": asks},
        "v2g_participation": {
            "granted_total": granted, "active": active, "withdrawn": withdrawn,
            "withdrawal_rate": _rate(withdrawn, granted),        # 철회율
        },
        "note": "추천·EV Bot 건수는 감사로그 기록 시작 이후 누적이다. 순편익·예측 오차는 /dashboard/data 참고. 합성·가정값 기반 모의 결과.",
    }
