"""이익공유(profit-sharing) 배분 시뮬레이터 — PRD 1차년도 산출물 "이익공유 모델
경제성 분석 기초자료"(WS4 "시민참여형 에너지 이익공유 서비스 BM 구체화"의 1차년도
몫)에 대응한다.

실제 결제·포인트 지급 체계(2차년도 BM 구체화 범위)는 아직 만들지 않는다 — 이미
계산된 실제 절감액(`compute_fleet_revenue_comparison`의 `savings_krw`, AI-V2G
최적화가 규칙기반 대비 실제로 아낀 전력비용)을 차주·충전인프라 운영자·플랫폼
사업자 배분 비율로 재구성해서 "얼마를 어떻게 나눌 수 있는가"를 보여주는 것까지만
한다.
"""

STAKEHOLDERS = ("owner", "operator", "platform")


class InvalidShareError(ValueError):
    """배분 비율에 음수가 있거나 합이 100%가 아닐 때."""


def compute_profit_sharing(savings_krw: float, owner_pct: float, operator_pct: float, platform_pct: float) -> dict:
    if any(p < 0 for p in (owner_pct, operator_pct, platform_pct)):
        raise InvalidShareError("배분 비율은 0 이상이어야 합니다.")
    total_pct = owner_pct + operator_pct + platform_pct
    if abs(total_pct - 100.0) > 0.01:
        raise InvalidShareError(f"배분 비율 합은 100%여야 합니다(현재 {total_pct}%).")

    total_savings_krw = max(float(savings_krw), 0.0)
    return {
        "total_savings_krw": round(total_savings_krw, 0),
        "shares_pct": {"owner": owner_pct, "operator": operator_pct, "platform": platform_pct},
        "amounts_krw": {
            "owner": round(total_savings_krw * owner_pct / 100, 0),
            "operator": round(total_savings_krw * operator_pct / 100, 0),
            "platform": round(total_savings_krw * platform_pct / 100, 0),
        },
        "note": (
            "실제 정산·포인트 지급 체계는 아직 없습니다 — AI-V2G가 실제로 만든 절감액을 "
            "배분 비율로 재구성해 보여주는 경제성 분석용 시뮬레이터입니다."
        ),
    }
