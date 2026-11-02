"""지역 인센티브 정산(포인트) 계산 — config/incentive_policy.json 정책을 방전 이벤트에 적용한다.

정책 요소
- points_per_kwh: 방전 1kWh당 기본 포인트
- peak_bonus: 피크 시간대(peak_hours)에 방전한 kWh에는 배율(multiplier)을 곱한다
- monthly_cap_points: 차량당 월 상한
- 실적 인정은 V2G/V2L 이벤트만(TEST 제외), 동의 확인은 호출 측이 consent.has_consent(ev, "incentive")로 거른다

정책 수치는 지자체 협의 전 예시값이다(verified=false) — 이 값으로 계산한 포인트는 '가정값'이다.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.paths import PROJECT_ROOT
from src.timeutil import to_kst_naive

POLICY_CONFIG = PROJECT_ROOT / "config" / "incentive_policy.json"
COUNTED_KINDS = ("V2G", "V2L")


@dataclass(frozen=True)
class IncentivePolicy:
    points_per_kwh: float
    peak_hours: tuple[int, ...]
    peak_multiplier: float
    monthly_cap_points: float | None
    krw_per_point: float
    verified: bool

    @classmethod
    def load(cls, path: Path | None = None) -> "IncentivePolicy":
        cfg = json.loads(Path(path or POLICY_CONFIG).read_text(encoding="utf-8"))
        pol = cls(
            points_per_kwh=float(cfg["points_per_kwh"]),
            peak_hours=tuple(int(h) for h in cfg.get("peak_hours", [])),
            peak_multiplier=float(cfg.get("peak_multiplier", 1.0)),
            monthly_cap_points=cfg.get("monthly_cap_points"),
            krw_per_point=float(cfg.get("krw_per_point", 1.0)),
            verified=bool(cfg.get("verified", False)),
        )
        if pol.points_per_kwh < 0 or pol.peak_multiplier < 1.0 or any(not 0 <= h <= 23 for h in pol.peak_hours):
            raise ValueError("인센티브 정책 값이 올바르지 않습니다")
        return pol


def compute_incentive(events: list[dict], policy: IncentivePolicy, eligible_evs: set[str] | None = None) -> dict:
    """events: discharge_events 행(dict) 목록. 월별·차량별 포인트를 계산하고 상한을 적용한다.
    한 이벤트가 시간대에 걸치면 시작 시각 기준 시간대로 판단한다(보수적 단순화)."""
    raw: dict[tuple[str, str], float] = defaultdict(float)
    skipped = defaultdict(int)
    for e in events:
        if e["kind"] not in COUNTED_KINDS:
            skipped["not_counted_kind"] += 1
            continue
        if eligible_evs is not None and e["ev_id"] not in eligible_evs:
            skipped["no_consent_or_not_eligible"] += 1
            continue
        ts = to_kst_naive(e["start_ts"])   # OCPP 이벤트는 UTC로 기록된다 — 피크 시간(한국시간)·월 경계는 변환 후 판단
        mult = policy.peak_multiplier if ts.hour in policy.peak_hours else 1.0
        raw[(e["ev_id"], ts.strftime("%Y-%m"))] += float(e["energy_kwh"]) * policy.points_per_kwh * mult

    rows, capped_any = [], False
    for (ev_id, month), pts in sorted(raw.items()):
        final = pts
        if policy.monthly_cap_points is not None and pts > policy.monthly_cap_points:
            final, capped_any = float(policy.monthly_cap_points), True
        rows.append({"ev_id": ev_id, "month": month, "points_before_cap": round(pts, 1),
                     "points": round(final, 1), "krw_equivalent": round(final * policy.krw_per_point, 0)})
    return {
        "rows": rows,
        "total_points": round(sum(r["points"] for r in rows), 1),
        "total_krw_equivalent": round(sum(r["krw_equivalent"] for r in rows), 0),
        "cap_applied": capped_any,
        "skipped": dict(skipped),
        "assumption": not policy.verified,  # True면 '가정값' 표기 필요
    }
