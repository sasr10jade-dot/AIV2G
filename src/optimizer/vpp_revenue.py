"""재원별 편익 산정 (요구사항 S01·Y2D01 — 계획서 p.33, p.48).

대시보드의 'VPP 수익원 포트폴리오'를 고정 비율(62/25/13%)이 아니라 재원별 산식으로 계산한다.

1) 요금 차익      = 일반충전 전력비용 - AI 최적 스케줄 전력비용 (열화비용 차감 후, compute_fleet_revenue_comparison)
2) 피크 저감      = Σ_월 (적용 전 거점 월 최대부하 - 적용 후 월 최대부하) x 기본요금 단가(원/kW·월)
3) 출력제어 완화  = Σ_t 과잉발전 시간 EV 흡수 전력(kWh) x 그 시각 전력 가치(원/kWh)   (curtailment.emulate)

단가·가정은 config/vpp_settlement.json(verified=false) — 결과는 '가정값'으로 표기한다.
"""
from __future__ import annotations

import pandas as pd


def compute_revenue_sources(revenue: dict, curtailment: dict, params: dict) -> dict:
    demand_charge = float(params["benefits"]["demand_charge_krw_per_kw_month"])
    hourly = curtailment["_hourly"]
    before = pd.Series(curtailment["_load_before"].to_numpy(), index=pd.to_datetime(hourly["timestamp"]))
    after = pd.Series(curtailment["_load_after"].to_numpy(), index=pd.to_datetime(hourly["timestamp"]))
    monthly = pd.DataFrame({"before_kw": before.resample("MS").max(), "after_kw": after.resample("MS").max()})
    monthly["reduction_kw"] = (monthly["before_kw"] - monthly["after_kw"]).clip(lower=0)
    peak_krw = float(monthly["reduction_kw"].sum() * demand_charge)

    tou_krw = float(revenue.get("savings_krw") or 0.0)
    curt_krw = float(curtailment["totals"]["curtailment_benefit_krw"])
    items = [
        {
            "key": "tou_arbitrage", "name": "충전 시간 이동·방전 요금 차익",
            "amount_krw": round(tou_krw, 0),
            "formula": "일반충전 전력비용 - AI 최적 스케줄 전력비용 - 배터리 열화비용",
            "basis": f"일반충전 {revenue.get('baseline_cost_krw', 0):,.0f}원 → AI {revenue.get('optimized_cost_krw', 0):,.0f}원",
        },
        {
            "key": "peak_reduction", "name": "거점 피크 저감(기본요금 절감)",
            "amount_krw": round(peak_krw, 0),
            "formula": "Σ월 (적용 전 월 최대부하 - 적용 후 월 최대부하) kW x 기본요금 단가",
            "basis": f"월평균 {monthly['reduction_kw'].mean():.1f} kW 저감 x {demand_charge:,.0f}원/kW·월 x {len(monthly)}개월",
        },
        {
            "key": "curtailment_relief", "name": "출력제어 완화(재생에너지 흡수)",
            "amount_krw": round(curt_krw, 0),
            "formula": "Σ 과잉발전 시간 EV 흡수 kWh x 해당 시각 전력 가치",
            "basis": f"흡수 {curtailment['totals']['absorbed_kwh']:,.1f} kWh, 출력제어 {curtailment['totals']['curtail_reduction_pct']}% 감소",
        },
    ]
    total = sum(max(i["amount_krw"], 0) for i in items)
    for i in items:
        i["share_pct"] = round(max(i["amount_krw"], 0) / total * 100, 1) if total > 0 else 0.0
    return {
        "items": items,
        "total_krw": round(total, 0),
        "monthly_peak": [
            {"month": idx.strftime("%Y-%m"), "before_kw": round(float(r.before_kw), 1), "after_kw": round(float(r.after_kw), 1),
             "reduction_kw": round(float(r.reduction_kw), 1)}
            for idx, r in monthly.iterrows()
        ],
        "verified": bool(params.get("verified")),
        "label": "가정값(모의)" if not params.get("verified") else "확정 단가",
        "sources": {
            "demand_charge": params["benefits"].get("demand_charge_source"),
            "curtailment_value": params["benefits"].get("curtailment_value_source"),
        },
    }
