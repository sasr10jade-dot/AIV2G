"""AI 최적 V2G 예측모델 모델 카드 (Model Card) — 사업수행계획서 1차년도 공식 산출물.

계획서 p.14, 16, 26~28 명세:
  - 성과지표: AI 기반 최적 V2G 예측모델 1건
  - 산출물: 모델 및 모델 설명 자료(모델 카드), 오차 분석 결과
  - 성능 목표(p.27): MAPE ≤20%, R² ≥0.70, Train/Test 80:20 분할 교차검증
  - 개발기관: (참여) ㈜아크릴(예측모델 1종 개발·검증), (주관) 아우토크립트㈜(데이터·정답 기준·최적화 연동)

성능 수치·표본 수·하이퍼파라미터·평가 구간은 고정값을 쓰지 않는다. `evaluate_plan_targets()`로
지금 데이터에 대해 실제로 학습·평가한 결과와 데이터 출처(실측/모의)를 받아 카드를 만든다.
"""
from __future__ import annotations

from typing import Any, Dict

TARGET_LABELS = {
    "ev_charging_demand_kw": "EV 충전수요 예측 (도시 시간별 kW)",
    "ev_available_v2g_kwh": "EV 가용 V2G 자원 예측 (선단 시간별 kWh)",
}

# 계획서 데이터 수집 계획상의 학습 데이터 출처(연계 목표). 실제 학습에 쓰인 데이터의 실측/모의 여부는
# training_data.actual_source 에 따로 적는다.
PLANNED_SOURCES = [
    {"id": "OD-01", "name": "환경공단 공공 충전기 상태 API (5분 스냅샷 기반 추정 세션)", "coverage": "도시 충전기 150기 상태 이력"},
    {"id": "OD-02", "name": "전력거래소(KPX) 시간별 전력수요 및 SMP", "coverage": "전국/지역 계통 수요·SMP"},
    {"id": "OD-03", "name": "KPX 지역·연료원별 재생에너지(태양광·풍력) 발전량", "coverage": "전남·광주 지역 발전 실적"},
    {"id": "OD-05", "name": "기상청 ASOS 관측 데이터 (기온 등)", "coverage": "광주 관측소 시간별"},
    {"id": "OD-06", "name": "공공데이터포털 특일 정보 (공휴일·대체공휴일)", "coverage": "연간"},
    {"id": "OBD-01", "name": "서포터즈 실차 30대 OBD2 단말 수집 데이터", "coverage": "SOC·주행/주차·플러그인 이력"},
]


def _status_text(ok: bool | None) -> str:
    if ok is None:
        return "산출 불가"
    return "목표 달성" if ok else "목표 미달"


def build_model_card(evaluations: Dict[str, dict] | None = None, provenance: dict | None = None) -> Dict[str, Any]:
    """모델 카드를 만든다.

    evaluations: {target_col: evaluate_plan_targets(...) 결과}. 없으면 성능 항목은 '평가 전'으로 둔다(값을 지어내지 않는다).
    provenance: src.pipeline.source.provenance() — 학습 데이터가 실측인지 모의(합성)인지.
    """
    evaluations = evaluations or {}
    provenance = provenance or {}
    # 예측 대상(EV 충전수요·가용자원)은 EV 텔레메트리에서 나오므로 그 표의 출처로 판정한다
    is_measured = provenance.get("ev_telemetry") == "measured"
    data_label = "실측" if is_measured else "모의(합성 시나리오)"

    primary = evaluations.get("ev_charging_demand_kw") or next(iter(evaluations.values()), None)
    target_rows = []
    for tc, ev in evaluations.items():
        t = ev["test"]
        target_rows.append({
            "target_col": tc,
            "target": TARGET_LABELS.get(tc, tc),
            "mape_pct": t["mape_pct"],
            "r2": t["r2"],
            "wape_pct": t["wape_pct"],
            "accuracy_pct": t.get("accuracy_pct"),
            "accuracy_met": ev["targets"].get("accuracy_met"),
            "cv_accuracy_pct": ev["cv"].get("accuracy_pct"),
            "rmse": t["rmse"],
            "cv_mape_pct": ev["cv"]["mape_pct"],
            "cv_r2": ev["cv"]["r2"],
            "mape_status": _status_text(ev["targets"]["mape_met"] if t["mape_pct"] is not None else None),
            "r2_status": _status_text(ev["targets"]["r2_met"] if t["r2"] is not None else None),
            "all_met": ev["targets"]["all_met"],
            "data_range": ev["data_range"],
            "n_train": ev["n_train"],
            "n_test": ev["n_test"],
        })

    return {
        "model_details": {
            "name": "AI-V2G 에너지 수요·공급 및 EV 가용자원 예측모델 (기준 모델)",
            "version": "1.0.0",
            "type": "시계열 회귀 (LightGBM Regressor, 달력·기상·자기회귀 피처)",
            "developed_by": {
                "lead_organization": "아우토크립트㈜ (학습 데이터셋 정제·정답 기준 수립·최적화 추론 연동)",
                "participating_organization": "㈜아크릴 (예측모델 1종 아키텍처 설계·학습·자체 GPU 검증)",
                "ai_platform": "㈜아크릴 통합 AI 플랫폼 '조나단(JONATHAN)' 연계 예정",
            },
            "license": "컨소시엄 공동 소유 (주관 아우토크립트㈜ / 참여 ㈜아크릴 협약 준수)",
            "status": f"1차년도 실증 검증 중 — 학습 데이터: {data_label}",
        },
        "intended_use": {
            "primary_uses": [
                "도시 충전기의 시간대별 충전 수요(kW) 예측",
                "서포터즈 EV 선단의 시간별 가용 V2G 자원(kWh) 예측",
                "V2G 의사결정 모델(LP 기반 충·방전 스케줄러)의 입력값 제공",
            ],
            "out_of_scope_uses": [
                "실차량 직접 물리 방전 제어 (1차년도는 실측 충전 기반 소프트웨어 모의 수행)",
                "비인가 충전기 강제 출력 제어",
            ],
        },
        "training_data": {
            "actual_source": data_label,
            "provenance": provenance,
            "planned_sources": PLANNED_SOURCES,
            "dataset_sources": PLANNED_SOURCES,   # 화면 호환용(계획상 출처)
            "total_samples": primary["n_rows"] if primary else None,
            "sample_unit": "도시 단위 시간별 행(피처 생성 후 결측 제거)",
            "preprocessing": [
                "품질 게이트(스키마·중복·범위 검사) 통과 데이터만 사용",
                "식별자 HMAC-SHA256 가명화",
                "달력(시·요일·월·주말·공휴일), 기상, 계통(수요·재생·요금), 자기회귀(1h·24h·168h 지연, 24h·168h 이동평균) 피처",
            ],
        },
        "evaluation_metrics": {
            "plan_targets": {"accuracy_min_pct": 70.0, "accuracy_formula": "예측 정확도 = (1 − WAPE) × 100",
                             "mape_max_pct": 20.0, "r2_min": 0.70, "split": "Train/Test 80:20 + 시간순 교차검증"},
            "evaluation_strategy": primary["method"] if primary else "평가 전",
            "data_range": primary["data_range"] if primary else None,
            "mape_note": primary["mape_note"] if primary else None,
            "metrics": {
                "wape_pct": primary["test"]["wape_pct"] if primary else None,
                "mape_pct": primary["test"]["mape_pct"] if primary else None,
                "rmse_kw": primary["test"]["rmse"] if primary else None,
                "r2_score": primary["test"]["r2"] if primary else None,
            },
            "target_evaluations": target_rows,
            "measured_or_inferred": f"성능은 {data_label} 데이터로 계산한 값",
        },
        "technical_specifications": {
            "algorithm": "LightGBM Regressor (조기종료)",
            "hyperparameters": primary["hyperparameters"] if primary else None,
            "features_used": primary["feature_cols"] if primary else [],
            "input_shape": f"(N, {len(primary['feature_cols'])})" if primary else None,
        },
        "caveats_and_limitations": {
            "technical_constraints": [
                "실제 EV 방전 인프라(양방향 충전기 및 V2G 양산차) 부재로 방전량은 가용 SOC 기반 추론(잠재량)으로 산출됨",
                "실차 수집 전 단계의 성능은 사전 검증용 모의(합성) 데이터 기준이며, KPI 실적으로 집계하지 않음",
                "시간별 충전수요는 심야 0 근처 값이 많아 MAPE가 크게 나오는 특성이 있음(실제값 평균 10% 미만 시간 제외 후 계산)",
                "2차년도에 하계 데이터를 포함한 누적 실측 데이터 재학습으로 고도화 예정",
            ],
            "labeling_rule": "모든 예측 및 시뮬레이션 결과 화면에 '실측'과 '모의(추론)'를 구분 표기함",
        },
    }
