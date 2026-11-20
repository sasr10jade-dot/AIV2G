"""성과·관리 지표판 — 평가의견 5·6·8·10·11 반영 문안을 한 화면에서 확인한다.

- 필수성과 5개(공고): EV 30대, 충전기 150기, 데이터셋 4,000건, AI 예측모델 1건, 예측 정확도 70% 이상
- 관리 지표 7개(평가의견 5): 데이터 유효율, AI 적용 효용성, 출력제어 시나리오, V2G 가용자원 추론, 모의 정산 PoC,
  참여자 VoC, 운행 영향
- 경제적 편익 4개 산출식(평가의견 11): 충전요금 절감액, 최대부하 시간대 충전 비중 변화, 가상 V2G 편익, 출력제어 완화 편익
- 원천별 데이터 건수와 성과 산입 여부(평가의견 8)
- V2G 비즈니스 레퍼런스 4종(평가의견 6), 실질 보상 단계(평가의견 10)

값은 모두 다른 모듈이 이미 계산한 결과를 모은 것이다(새 산식 없음). 실측·추론·모의 구분을 항목마다 붙인다.
"""
from __future__ import annotations

from src import kpi


def _status(value, target, higher_is_better: bool = True):
    if value is None or target is None:
        return None
    return value >= target if higher_is_better else value <= target


def build_board(b: dict, accuracy: dict | None = None, settlement: dict | None = None, surveys: list | None = None,
                ess_aggregate: dict | None = None, incentives: dict | None = None) -> dict:
    """b = vpp.bundle() 결과. accuracy = 모델 평가(있으면), settlement = ledger.summary(), surveys = 설문 목록."""
    from src.optimizer.effect_metrics import peak_metrics

    ev_df, charger_df, prov = b["ev_df"], b["charger_df"], b.get("provenance") or {}
    summ = kpi.summarize_kpi(ev_df, charger_df, b.get("grid_df"), provenance=prov)
    y1 = next((y for y in summ.official_by_year if y.year == "Y1"), None)
    y1_all = next((y for y in summ.by_year if y.year == "Y1"), None)
    measured = prov.get("ev_telemetry") == "measured" or prov.get("charger_sessions") == "measured"
    data_label = "실측" if measured else "모의(합성 데이터)"

    acc = (accuracy or {}).get("accuracy_pct")
    mandatory = [
        {"id": "ev", "label": "실증 EV", "value": summ.ev_count, "target": kpi.TARGET_EV, "unit": "대",
         "met": _status(summ.ev_count, kpi.TARGET_EV), "basis": data_label},
        {"id": "chargers", "label": "연계 충전기", "value": summ.charger_count, "target": kpi.TARGET_CHARGERS, "unit": "기",
         "met": _status(summ.charger_count, kpi.TARGET_CHARGERS), "basis": data_label},
        {"id": "dataset", "label": "1차년도 데이터셋", "value": (y1.records if measured and y1 else (y1_all.records if y1_all else 0)),
         "target": kpi.DATA_PLAN["target"], "unit": "건",
         "met": _status(y1.records if measured and y1 else None, kpi.DATA_PLAN["target"]),
         "basis": "실측 실적" if measured else "모의 미리보기(실적 아님)"},
        {"id": "model", "label": "AI 예측모델", "value": 1, "target": 1, "unit": "건", "met": True, "basis": "LightGBM 기준 모델(모델 카드)"},
        {"id": "accuracy", "label": "예측 정확도 (1−WAPE)×100", "value": acc, "target": 70.0, "unit": "%",
         "met": _status(acc, 70.0), "basis": (accuracy or {}).get("basis") or "모델 카드 평가 후 표시"},
    ]

    # ---- 관리 지표 7개
    f = kpi.session_funnel(charger_df, kpi._supporter_ids(ev_df))
    validity = round(f["valid"] / f["raw"] * 100, 1) if f["raw"] else None
    rev = b.get("revenue") or {}
    sc = {r["id"]: r for r in ((b.get("curtailment") or {}).get("scenarios") or {}).get("rows", [])}
    s3 = sc.get("S3", {})
    agg = (ess_aggregate or {}).get("summary") or {}
    st = settlement or {}
    surveys = surveys or []
    management = [
        {"id": "validity", "label": "데이터 유효율", "value": validity, "unit": "%",
         "detail": f"충전 세션 원천 {f['raw']:,} → 유효 {f['valid']:,} → 중복 제거 {f['deduplicated']:,} → 성과 산입 {f['counted']:,}", "kind": data_label},
        {"id": "ai_effect", "label": "AI 적용 효용성", "value": rev.get("savings_pct"), "unit": "% 충전비 절감",
         "detail": f"일반 {rev.get('baseline_cost_krw', 0):,.0f}원 → AI {rev.get('optimized_cost_krw', 0):,.0f}원", "kind": "추론(같은 데이터에 AI 적용)"},
        {"id": "curtailment", "label": "출력제어 시나리오", "value": s3.get("curtail_relief_kwh"), "unit": "kWh 완화(S3)",
         "detail": f"피크 수전 감소 {s3.get('peak_grid_reduction_kw', '-')}kW · 흡수 {s3.get('absorbed_kwh', '-')}kWh", "kind": "모의(가정값)"},
        {"id": "v2g_resource", "label": "V2G 가용자원 추론", "value": agg.get("avg_capacity_kwh"), "unit": "kWh(ESS 그룹 평균)",
         "detail": f"평균 출력 {agg.get('avg_power_kw', '-')}kW · 최대 동시 가용 {agg.get('max_concurrent', '-')}대", "kind": "추론"},
        {"id": "settlement", "label": "모의 정산 PoC", "value": st.get("total_points"), "unit": "P 확정",
         "detail": f"확정 대기 {st.get('pending_points', 0):,}P · 미답변 문의 {st.get('open_inquiries', 0)}건", "kind": "모의"},
        {"id": "voc", "label": "참여자 VoC", "value": sum(int(s.get("responses") or 0) for s in surveys), "unit": "건 응답",
         "detail": f"설문 {len(surveys)}개", "kind": "실측(설문 응답)"},
        {"id": "driving", "label": "운행 영향", "value": s3.get("min_soc_violations"), "unit": "건 최소 SOC 미달(S3)",
         "detail": "방전 시나리오에서 최소 보장 SOC 아래로 내려가는 차량-시간(보수적 계산). 0건이 목표", "kind": "추론"},
    ]

    # ---- 경제적 편익 4개 산출식(평가의견 11)
    pm = peak_metrics(b["comparison"], b["grid_df"]) if "comparison" in b else {}
    src_items = {i["key"]: i for i in (b.get("revenue_sources") or {}).get("items", [])}
    curt_tot = (b.get("curtailment") or {}).get("totals") or {}
    benefits = [
        {"id": "charge_saving", "label": "충전요금 절감액", "value": rev.get("savings_krw"), "unit": "원",
         "formula": "Σ(일반 충전 시각별 충전량 × 계시별 단가) − Σ(AI 충전 시각별 충전량 × 계시별 단가)"},
        {"id": "peak_share", "label": "최대부하 시간대 충전 비중 변화",
         "value": (round(pm["optimized_peak_charge_share_pct"] - pm["baseline_peak_charge_share_pct"], 1) if pm else None), "unit": "%p",
         "formula": f"AI 피크 시간 충전 비중 − 일반 피크 시간 충전 비중 ({pm.get('peak_definition', '')}: "
                    f"{pm.get('baseline_peak_charge_share_pct', '-')}% → {pm.get('optimized_peak_charge_share_pct', '-')}%)"},
        {"id": "virtual_v2g", "label": "가상 V2G 편익", "value": (src_items.get("peak_reduction") or {}).get("amount_krw"), "unit": "원",
         "formula": (src_items.get("peak_reduction") or {}).get("formula") or "Σ월 피크 저감 kW × 기본요금 단가"},
        {"id": "curtail_relief", "label": "출력제어 완화 편익", "value": curt_tot.get("curtailment_benefit_krw"), "unit": "원",
         "formula": "Σ 과잉발전 시간 EV 흡수 kWh × 해당 시각 전력 가치(SMP)"},
    ]

    # ---- 원천별 데이터(평가의견 8)
    n_obd = kpi.count_vehicle_sessions(ev_df)
    public_ref = summ.public_reference_records
    sources = [
        {"source": "서포터즈 EV 차량 OBD 세션", "count": n_obd, "counted": True, "kind": prov.get("ev_telemetry", "-")},
        {"source": "CPO 3사 거점 유효 충전 세션(서포터즈 차량 세션 제외)", "count": f["counted"], "counted": True, "kind": prov.get("charger_sessions", "-")},
        {"source": "서포터즈 차량의 CPO 세션(OBD와 중복)", "count": f["supporter_excluded"], "counted": False, "kind": "중복 제외"},
        {"source": "CPO 150기 전체 이용 패턴(충전기 상태·이용률)", "count": summ.charger_count, "counted": False, "kind": "보조(충전기 수)"},
        {"source": "관용차량(관리기관 협조, 서포터즈에 포함 시 OBD로 산입)", "count": None, "counted": None, "kind": "협조 요청 중"},
        {"source": "공공데이터(전력수요·재생에너지·SMP·기상)·타 지역 비교", "count": public_ref, "counted": False, "kind": "보조(참고 건수)"},
    ]

    references = [
        {"id": "revenue", "label": "실증 데이터 기반 가상 V2G 수익 산정", "status": "시스템 산출", "where": "대시보드 05 · 재원별 편익"},
        {"id": "settlement", "label": "제도·정산 레퍼런스(모의 정산 PoC)", "status": "시스템 산출", "where": "대시보드 05 · 모의 포인트 정산, 관용차 기관 리포트"},
        {"id": "voc", "label": "참여주체 VoC", "status": "시스템 수집", "where": "웹앱 관리 · 설문(VoC)"},
        {"id": "benchmark", "label": "국내외 V2G 사업 사례 벤치마크", "status": "문서 산출물(시스템 외)", "where": "레퍼런스 보고서"},
    ]

    disc = next((o for o in (incentives or {}).get("options", []) if o.get("type") == "charge_discount"), None)
    compensation = [
        {"stage": 1, "label": "사업 종료 후 즉시 — AI 추천 시간 충전분 실제 충전요금 할인(CPO 합의)", "status": "모의 산정",
         "estimate": disc and {"participant_krw": disc.get("participant_krw"), "per_vehicle_krw": disc.get("per_vehicle_krw"), "label": disc.get("label")}},
        {"stage": 2, "label": "수요자원(DR) 시장 참여 후 — DR 보상 배분", "status": "제도 확인 후", "estimate": None},
        {"stage": 3, "label": "방전 계량·정산 기준 마련 후 — V2G 방전 수익 배분", "status": "제도 마련 후", "estimate": None},
    ]
    return {"data_label": data_label, "mandatory": mandatory, "management": management, "benefits": benefits,
            "benefit_basis": "한전 전기차 충전전력요금 계절·시간대 단가(config/tariffs.json)와 SMP 적용 — verified=false면 가정값",
            "data_sources": sources, "references": references, "compensation": compensation,
            "data_plan": kpi.DATA_PLAN}
