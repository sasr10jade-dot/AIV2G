"""탄소배출 절감 효과 추정 — PRD "도시 에너지·시민 편익 효과 검증"의 환경 임팩트
지표. AI-V2G 최적화와 재생에너지 발전이 각각 회피한 온실가스 배출량을 추정한다.

**단순화 전제(참고용 추정치)**: 시간대별로 변하는 실제 계통 탄소집약도(발전원 믹스)
데이터가 없어, 고정된 평균 배출계수 하나를 전 기간에 곱한다 — 이미 전력가격 프록시가
같은 수준으로 단순화돼 있는 것과 동일한 한계다. 실제 실증에서는 KPX/전력거래소가
공개하는 시간대별 배출계수 연동으로 정확도를 높일 수 있다.
"""
import pandas as pd

# 2022년 한국전력 발표 전력 1kWh당 온실가스 배출계수(국가 평균 그리드 믹스 기준).
# 매년 갱신되는 값이라 정확한 실측이 필요하면 최신 공식 계수로 교체해야 한다.
GRID_EMISSION_FACTOR_KG_PER_KWH = 0.4781


def compute_carbon_reduction(
    comparison: pd.DataFrame, grid_df: pd.DataFrame, emission_factor: float = GRID_EMISSION_FACTOR_KG_PER_KWH
) -> dict:
    """`comparison`(baseline_kw/optimized_kw, 시간당)과 `grid_df`(renewable_gen_mw)로
    두 가지 회피 배출량을 계산한다.

    - `optimization_co2_kg`: AI-V2G가 방전으로 그리드에서 "덜 끌어온" 순에너지
      (sum(baseline_kw) - sum(optimized_kw), 시간당 kW = kWh)에 배출계수를 곱한 값.
      단순히 충전 시점만 옮기는 밸리필링/피크저감은 총 에너지량 자체를 바꾸지 않으므로
      (고정 배출계수 가정 하에서는) 이 지표에 기여하지 않는다 — 실제로 그리드에 에너지를
      되돌려준(V2G 방전) 만큼만 반영된다.
    - `renewable_co2_kg`: 재생에너지 발전량(MWh)이 그만큼의 화석연료 발전을
      대체했다고 가정한 회피 배출량.
    """
    net_energy_saved_kwh = max(float((comparison["baseline_kw"] - comparison["optimized_kw"]).sum()), 0.0)
    optimization_co2_kg = net_energy_saved_kwh * emission_factor

    renewable_mwh = float(grid_df["renewable_gen_mw"].sum())
    renewable_co2_kg = renewable_mwh * 1000.0 * emission_factor

    total_co2_kg = optimization_co2_kg + renewable_co2_kg
    return {
        "emission_factor_kg_per_kwh": emission_factor,
        "net_energy_saved_kwh": round(net_energy_saved_kwh, 1),
        "renewable_mwh": round(renewable_mwh, 1),
        "optimization_co2_kg": round(optimization_co2_kg, 1),
        "renewable_co2_kg": round(renewable_co2_kg, 1),
        "total_co2_kg": round(total_co2_kg, 1),
        "total_co2_ton": round(total_co2_kg / 1000.0, 2),
    }
