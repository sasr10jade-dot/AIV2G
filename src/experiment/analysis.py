"""실험 효과 분석 — 실험군 대비 대조군의 평균 차이와 부트스트랩 신뢰구간.

차량 수가 적은 실증(30대)에서는 단순 평균 비교의 불확실성이 크므로, 점추정만 내놓지 않고
95% 신뢰구간과 표본 수를 함께 돌려준다. 구간이 0을 포함하면 "효과 있음"이라고 말하지 않는다.
"""
from __future__ import annotations

import numpy as np

MIN_PER_GROUP = 3  # 이보다 적으면 비교 자체를 하지 않는다


def difference_in_means(treatment: list[float], control: list[float], n_boot: int = 4000,
                        seed: int = 0, alpha: float = 0.05) -> dict:
    t, c = np.asarray(treatment, dtype=float), np.asarray(control, dtype=float)
    if len(t) < MIN_PER_GROUP or len(c) < MIN_PER_GROUP:
        return {"ok": False, "reason": f"그룹당 최소 {MIN_PER_GROUP}대 필요", "n_treatment": len(t), "n_control": len(c)}
    rng = np.random.default_rng(seed)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        boots[i] = rng.choice(t, len(t)).mean() - rng.choice(c, len(c)).mean()
    lo, hi = np.quantile(boots, [alpha / 2, 1 - alpha / 2])
    diff = float(t.mean() - c.mean())
    return {
        "ok": True, "diff": round(diff, 3), "ci_low": round(float(lo), 3), "ci_high": round(float(hi), 3),
        "n_treatment": len(t), "n_control": len(c),
        "significant": bool(lo > 0 or hi < 0),
    }
