"""충·방전 계획의 독립 안전 검증 (AI 신뢰체계 6.1.3 위험처리: 최소 SoC·방전 한도·수동 중단).

최적화기(`soc_planner`)가 제약을 걸어 풀더라도, 그 결과가 이용자 조건을 지키는지 **최적화기와 별개의 코드로 다시 확인**한다.
AI(최적화)는 경제성을 높이는 계층이고, 이동권·배터리 안전 조건을 대체하거나 완화하지 못한다. 검증을 하나라도 통과하지 못하면
호출 측이 방전 없는 계획(V1G)으로 되돌린다.

검증 항목(각각 id·통과 여부·설명을 돌려준다 — 화면과 운영 로그에 그대로 쓴다):
  departure_soc   출발 시점 잔량 ≥ 이용자가 정한 필요 잔량
  reserve_floor   모든 시각의 잔량 ≥ 예비 잔량(이동 중 비상 여유)
  capacity_cap    모든 시각의 잔량 ≤ 배터리 용량
  discharge_band  방전은 허용 범위(lo~hi) 안의 잔량에서만
  no_simultaneous 같은 시간에 충전과 방전을 동시에 하지 않음
  participation   방전 참여를 끈 이용자에게는 방전을 계획하지 않음
"""
from __future__ import annotations

import numpy as np

EPS = 1e-6


def verify_plan(energy, charge, discharge, cap_kwh: float, target_kwh: float, reserve_kwh: float,
                band_kwh: tuple[float, float], participating: bool = True) -> dict:
    """energy[0..n]는 시각별 잔량(kWh, 길이 n+1), charge·discharge[0..n-1]는 시간별 충·방전 kWh."""
    e = np.asarray(energy, dtype=float)
    c = np.asarray(charge, dtype=float)
    d = np.asarray(discharge, dtype=float)
    lo, hi = band_kwh
    pct = (lambda kwh: kwh / cap_kwh * 100.0) if cap_kwh > 0 else (lambda kwh: 0.0)

    d_idx = np.where(d > 0.05)[0]
    bad_band = [int(i) for i in d_idx if not (lo - EPS <= e[i] <= hi + EPS)]
    both = [int(i) for i in range(len(d)) if c[i] > 0.05 and d[i] > 0.05]

    checks = [
        {"id": "departure_soc", "ok": bool(e[-1] >= target_kwh - EPS),
         "detail": f"출발 시 {pct(e[-1]):.0f}% / 필요 {pct(target_kwh):.0f}%"},
        {"id": "reserve_floor", "ok": bool(e.min() >= reserve_kwh - EPS),
         "detail": f"최저 {pct(e.min()):.0f}% / 예비 {pct(reserve_kwh):.0f}%"},
        {"id": "capacity_cap", "ok": bool(e.max() <= cap_kwh + EPS),
         "detail": f"최고 {pct(e.max()):.0f}% / 용량 100%"},
        {"id": "discharge_band", "ok": not bad_band,
         "detail": f"방전은 {pct(lo):.0f}~{pct(hi):.0f}% 구간에서만" if not bad_band else f"허용 범위를 벗어난 방전 {len(bad_band)}건"},
        {"id": "no_simultaneous", "ok": not both, "detail": "충전·방전 동시 없음" if not both else f"동시 {len(both)}건"},
        {"id": "participation", "ok": bool(participating or not len(d_idx)),
         "detail": "방전 참여 중" if participating else "방전 참여 꺼짐 — 방전 계획 없음"},
    ]
    return {"passed": all(x["ok"] for x in checks), "checks": checks}
