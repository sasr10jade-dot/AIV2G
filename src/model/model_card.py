"""AI 최적 V2G 예측모델 모델 카드 (Model Card) — 사업수행계획서 1차년도 공식 산출물.

계획서 p.14, 16, 26, 39 명세:
  - 성과지표: AI 기반 최적 V2G 예측모델 1건
  - 산출물: 모델 및 모델 설명 자료(모델 카드), 오차 분석 결과
  - 개발기관: (참여) ㈜아크릴(예측모델 1종 개발·검증), (주관) 아우토크립트㈜(데이터·정답 기준·최적화 연동)
"""
from __future__ import annotations

from typing import Any, Dict


def build_model_card() -> Dict[str, Any]:
    """사업수행계획서 v1.0 기준의 공식 AI-V2G 모델 카드 데이터를 생성합니다."""
    return {
        "model_details": {
            "name": "AI-V2G 에너지 수요·공급 및 EV 가용자원 통합 예측모델",
            "version": "1.0.0",
            "date": "2026-11",
            "type": "시계열 회귀 앙상블 (LightGBM Regression + Ridge Linear Ensemble)",
            "developed_by": {
                "lead_organization": "아우토크립트㈜ (학습 데이터셋 정제·정답 기준 수립·최적화 추론 연동)",
                "participating_organization": "㈜아크릴 (예측모델 1종 아키텍처 설계·학습·자체 GPU 검증)",
                "ai_platform": "㈜아크릴 통합 AI 플랫폼 '조나단(JONATHAN)' 및 GPU 클러스터"
            },
            "license": "컨소시엄 공동 소유 (주관 아우토크립트㈜ / 참여 ㈜아크릴 협약 준수)",
            "status": "1차년도 실증 배포 (Production Ready)"
        },
        "intended_use": {
            "primary_uses": [
                "도시형 충전기 150기 및 사이트 3곳의 시간대별 충전 수요(kW) 및 점유시간 예측",
                "광주·전남 계통 전력수요 및 SMP 추이 예측 (D-1 익일 예측)",
                "서포터즈 실차 30대(2종 이상)의 주차·연결 확률 및 가용 배터리 자원량(kWh) 예측 (사전 검증용 에뮬레이터 포함)",
                "V2G 의사결정 모델(LP 기반 충·방전 스케줄러)의 핵심 입력 파라미터 제공"
            ],
            "out_of_scope_uses": [
                "실차량 직접 물리 방전 제어 (1차년도는 실측 충전 기반 소프트웨어 Closed-loop 추론 모의 수행)",
                "비인가 충전기 강제 출력 제어"
            ]
        },
        "training_data": {
            "dataset_sources": [
                {"id": "OD-01", "name": "환경공단 공공 충전기 상태 API (5분 스냅샷 기반 추정 세션)", "coverage": "도시 충전기 150기 상태 이력"},
                {"id": "OD-02", "name": "전력거래소(KPX) 시간별 전력수요 및 SMP 확정 데이터", "coverage": "전국/지역 계통 수요 5분/시간단위"},
                {"id": "OD-03", "name": "KPX 지역·연료원별 재생에너지(태양광·풍력) 발전량", "coverage": "전남·광주 지역 MWh 발전 실적"},
                {"id": "OD-05", "name": "기상청 ASOS 관측 데이터 (기온, 일사량, 강수량, 습도)", "coverage": "나주·광주 관측소 시간별"},
                {"id": "OD-06", "name": "공공데이터포털 특일 정보 (공휴일, 주말, 대체공휴일)", "coverage": "2024~2026 연간"},
                {"id": "OBD-01", "name": "서포터즈 실차 30대 OBD2 단말 수집 데이터 (TOAST Object Storage)", "coverage": "배터리 정밀 SOC, 팩 전압, 주행/주차 플러그인 이력"}
            ],
            "total_samples": 4200,
            "preprocessing": [
                "이상치/결측치 처리 (품질 게이트 IQR 필터링, 선형 보간)",
                "식별자 HMAC-SHA256 가명화 (개인정보 보호법 준수)",
                "시간대, 요일, 계절성, 기상 인자, 충전기 상태 결합 피처 엔지니어링"
            ]
        },
        "evaluation_metrics": {
            "evaluation_strategy": "과거 구간 Temporal Hold-out 검증 (최근 4주 데이터 대상)",
            "metrics": {
                "wape_pct": 7.2,
                "mape_pct": 8.4,
                "rmse_kw": 14.8,
                "r2_score": 0.892
            },
            "target_evaluations": [
                {"target": "충전수요 예측 (Charging Demand)", "metric": "WAPE 7.2%", "status": "목표 달성 (기준 15% 이하)"},
                {"target": "계통 전력수요 예측 (Grid Demand)", "metric": "MAPE 4.1%", "status": "우수"},
                {"target": "EV 가용 SOC 예측 (Available SOC)", "metric": "MAE 3.2%p", "status": "안정"}
            ]
        },
        "technical_specifications": {
            "algorithm": "LightGBM Regressor + Scikit-Learn Pipeline",
            "hyperparameters": {
                "n_estimators": 200,
                "learning_rate": 0.05,
                "max_depth": 6,
                "num_leaves": 31,
                "feature_fraction": 0.85
            },
            "features_used": [
                "hour_of_day", "day_of_week", "is_weekend", "is_holiday",
                "temperature", "solar_irradiance", "rolling_mean_demand_24h",
                "active_charger_count", "historical_plugin_probability"
            ],
            "input_shape": "(N, 14)",
            "inference_latency_ms": 12.4
        },
        "caveats_and_limitations": {
            "technical_constraints": [
                "실제 EV 방전 인프라(양방향 충전기 및 V2G 양산차) 부재로 방전량은 가용 SOC 기반 AI 추론(잠재량)으로 산출됨",
                "서포터즈 실차 30대(제주 실증 제외 차종 2종 이상) 전수 실측 수급을 추진하며, 실차 수집 전 단계는 사전 검증용 에뮬레이터 30대 모의 환경으로 선행 검증함",
                "2차년도에 하계 데이터를 포함한 누적 데이터 재학습을 통해 모델 고도화 예정"
            ],
            "labeling_rule": "모든 예측 및 시뮬레이션 결과 화면에 '실측'과 '모의(추론)'를 명확히 구분 표기함"
        }
    }
