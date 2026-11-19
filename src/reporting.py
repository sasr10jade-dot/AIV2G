"""결과 자료 엑셀 — 계획서 산출물(데이터 확보 보고서·효용성 검증 결과·이익공유 경제성 기초자료)의 근거 표.

대시보드와 같은 계산 결과(`build_dashboard_payload`)를 그대로 시트로 옮긴다. 화면과 숫자가 어긋나지 않게 새로 계산하지 않는다.
모든 시트 위쪽에 데이터 성격(실측/모의)과 값의 성격(실측·추론)을 적는다 — 합성 데이터가 실적처럼 쓰이지 않게.
"""
from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from src.kpi import PROJECT_YEARS

NAVY = "0F2A5C"
TINT = "E8F0FD"
HEAD_FONT = Font(name="Noto Sans KR", bold=True, color="FFFFFF")
BODY_FONT = Font(name="Noto Sans KR")
TITLE_FONT = Font(name="Noto Sans KR", bold=True, size=14, color=NAVY)
NOTE_FONT = Font(name="Noto Sans KR", size=9, color="4A5875")
PROVENANCE = {"measured": "실측", "public": "공개 데이터", "synthetic": "모의(합성)"}


def _sheet(wb: Workbook, title: str, heading: str, notes: list[str]):
    ws = wb.create_sheet(title)
    ws["A1"] = heading
    ws["A1"].font = TITLE_FONT
    for i, n in enumerate(notes, start=2):
        ws.cell(row=i, column=1, value=n).font = NOTE_FONT
    ws._next_row = len(notes) + 3          # 표가 시작할 행
    return ws


def _table(ws, headers: list[str], rows: list[list], title: str | None = None) -> None:
    r = ws._next_row
    if title:
        ws.cell(row=r, column=1, value=title).font = Font(name="Noto Sans KR", bold=True, color=NAVY)
        r += 1
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=r, column=c, value=h)
        cell.font, cell.fill = HEAD_FONT, PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in rows:
        r += 1
        for c, v in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=v).font = BODY_FONT
    ws._next_row = r + 2
    for c, h in enumerate(headers, start=1):
        width = max([len(str(h))] + [len(str(row[c - 1])) for row in rows if c - 1 < len(row) and row[c - 1] is not None]) * 1.6 + 2
        letter = get_column_letter(c)
        ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, min(width, 60))


def build_results_workbook(payload: dict) -> bytes:
    kpi = payload["kpi"]
    prov = kpi.get("data_provenance") or {}
    is_real = bool(kpi.get("data_is_real"))
    nature = "실증 데이터(실측)" if is_real else "모의(합성) 데이터 — 성과 실적 아님, 화면·산식 확인용"
    common = [f"생성 시각: {payload.get('generated_at', '')}", f"데이터 성격: {nature}",
              "표별 출처: " + ", ".join(f"{k} {PROVENANCE.get(v, v)}" for k, v in prov.items())]

    wb = Workbook()
    wb.remove(wb.active)

    # 1. 성과지표(데이터 확보 보고서)
    ws = _sheet(wb, "성과지표", "데이터 확보 현황 — 사업수행계획서 성과지표", common + [
        "1건 = 연계 충전기의 유효 충전 세션 1건 또는 주행·충전·연결이 관측된 (차량, 날짜) 1건. 연차별 미중복.",
        "공백기(2027-04-16~04-30)·사업기간 밖·합성 데이터·방전(추론)은 실적에 넣지 않는다. 공개 데이터는 참고 건수."])
    official = {y["year"]: y for y in kpi.get("data_official_by_year", [])}
    _table(ws, ["연차", "기간", "목표(건)", "실적(건, 실측만)", "달성률(%)", "전체 집계(건, 모의 포함)", "충전 세션", "차량 세션"],
           [[y["label"], f"{PROJECT_YEARS[y['year']]['start']} ~ {PROJECT_YEARS[y['year']]['end']}", y["target"],
             official.get(y["year"], {}).get("records", 0), official.get(y["year"], {}).get("achieved_pct", 0.0),
             y["records"], y["charger_sessions"], y["vehicle_days"]] for y in kpi.get("data_by_year", [])], "연차별 데이터 건수")
    tg = kpi.get("targets") or {}
    infra = kpi.get("infra") or {}
    _table(ws, ["항목", "현재", "목표"], [
        ["실증 EV(대)", kpi.get("ev_count"), tg.get("ev")],
        ["EV 차종(종)", kpi.get("ev_model_count"), tg.get("ev_models")],
        ["연계 충전기(기)", kpi.get("charger_count"), tg.get("chargers")],
        ["충전기 종류(종)", kpi.get("charger_type_count"), tg.get("charger_types")],
        ["충전서비스 사업자(사)", kpi.get("operator_count"), tg.get("operators")],
        ["참여 장소(곳)", infra.get("site_count"), tg.get("sites")],
        ["활동 차량(최근 7일, 대)", (infra.get("vehicles") or {}).get("active"), tg.get("ev")],
        ["공개 데이터 참고 건수", kpi.get("public_reference_records"), "-"],
        ["사업기간 밖·공백기 건수(제외)", kpi.get("data_out_of_period"), "-"],
    ], "실증 인프라")

    # 2. 일자별 수집·전망
    col = kpi.get("collection") or {}
    ws = _sheet(wb, "일자별 수집", "일자별 유효 데이터와 수집 속도 전망", common + [
        "전망 = 현재 누적 + 최근 14일 하루 평균 × 연차 종료일까지 남은 일수." + (" (모의 미리보기)" if col.get("preview") else "")])
    p = col.get("pace")
    if p:
        _table(ws, ["연차", "기준일", "누적(건)", "최근 14일 하루 평균", "남은 일수", "종료 시 예상(건)", "목표(건)", "목표 대비(%)", "목표까지 하루 필요"],
               [[p["label"], p["as_of"], p["counted"], p["per_day"], p["remaining_days"], p["expected"], p["target"], p["expected_pct"], p["per_day_needed"]]],
               "수집 속도 전망")
    _table(ws, ["날짜", "충전 세션", "차량 세션", "합계"],
           [[d["date"], d["charger_sessions"], d["vehicle_days"], d["charger_sessions"] + d["vehicle_days"]] for d in col.get("daily", [])],
           "일자별 건수(최근 60일)")

    # 3. 실증 인프라
    ws = _sheet(wb, "실증 인프라", "참여 장소·충전사업자·서포터즈 차량", common + ["장소 이름·주소는 관리기관 협의 전 예시(config/sites.json)."])
    _table(ws, ["장소 ID", "이름", "유형", "주소", "충전기(기)", "유효 세션", "사업자", "확정"],
           [[s["site_id"], s.get("name"), s.get("type"), s.get("address"), s["chargers"], s["sessions"], ", ".join(s.get("operators") or []),
             "확정" if s.get("confirmed") else "협의 중"] for s in infra.get("sites", [])], "참여 장소")
    _table(ws, ["사업자", "충전기(기)", "종류", "유효 세션"],
           [[o["operator_id"], o["chargers"], ", ".join(f"{k} {v}" for k, v in (o.get("charger_types") or {}).items()), o["sessions"]]
            for o in infra.get("operators", [])], "충전사업자별 충전기")
    _table(ws, ["차량 ID", "차종", "상태", "마지막 관측", "충전한 날(최근 4주)", "주당 충전", "최근 배터리(%)"],
           [[r["ev_id"], r.get("vehicle_model"), "활동" if r["active"] else "휴면", r.get("last_seen"), r["charge_days_4w"], r["charge_per_week"], r.get("last_soc_pct")]
            for r in col.get("roster", [])], "서포터즈 차량별 현황")

    # 4. 데이터 품질
    ws = _sheet(wb, "데이터 품질", "데이터 품질 검증(필수 항목·값 범위·중복)", common)
    _table(ws, ["표", "건수", "판정"], [[q["table"], q["n_rows"], "PASS" if q["passed"] else "FAIL"] for q in kpi.get("quality", [])])

    # 5. 효용성 검증
    eff = payload.get("effect_verification") or {}
    ws = _sheet(wb, "효용성 검증", "일반 충전 대비 AI 적용 효용성 검증(계획서 4.1 다)", common + [
        "적용 후 값은 같은 실측 데이터에 AI 최적화를 적용한 추론값이며 실제 방전이 아니다.",
        f"피크 시간대: {(eff.get('peak') or {}).get('peak_definition', '')}. V2G 예측률 = (1 − WAPE) × 100, 출처: {eff.get('v2g_prediction_source', '-')}"])
    pk, cost, tr = eff.get("peak") or {}, eff.get("cost") or {}, eff.get("target_reach") or {}
    _table(ws, ["지표", "일반 충전(적용 전)", "AI 최적화(적용 후, 추론)", "비고"], [
        ["피크 시간대 충전 비중(%)", pk.get("baseline_peak_charge_share_pct"), pk.get("optimized_peak_charge_share_pct"), ""],
        ["선단 최대 부하(kW)", pk.get("baseline_max_load_kw"), pk.get("optimized_max_load_kw"), ""],
        ["충전 비용(원)", cost.get("baseline_cost_krw"), cost.get("optimized_cost_krw"), f"절감 {cost.get('savings_krw')}원({cost.get('savings_pct')}%)"],
        ["목표 배터리 도달률(%)", tr.get("baseline_reach_pct"), tr.get("optimized_reach_pct"), f"목표 {tr.get('target_soc_pct')}% · 세션 {tr.get('sessions')}"],
        ["V2G 예측률(%)", "-", eff.get("v2g_prediction_rate_pct"), "추론"],
        ["방전 추론량(kWh)", 0, cost.get("discharge_kwh"), "추론, 실적 아님"],
    ], "적용 전·후 비교")
    cond = eff.get("conditions") or {}
    for key, name in (("time_band", "충전 시작 시간대"), ("day_type", "평일·주말"), ("start_soc", "시작 배터리"), ("vehicle_type", "차량 운행조건")):
        _table(ws, [name, "세션", "적용 전 비용(원)", "적용 후 비용(원)", "절감(원)", "절감률(%)", "방전 추론(kWh)"],
               [[r["label"], r["sessions"], r["baseline_cost_krw"], r["optimized_cost_krw"], r["savings_krw"], r["savings_pct"], r["discharge_kwh"]]
                for r in cond.get(key, [])], f"조건별 분석 — {name}")
    _table(ws, ["전력수요 수준", "시간 수", "적용 전 충전 비중(%)", "적용 후 충전 비중(%)"],
           [[r["label"], r["hours"], r["baseline_charge_share_pct"], r["optimized_charge_share_pct"]] for r in cond.get("power_demand", [])],
           "조건별 분석 — 전력수요 수준(3분위)")

    # 6. 이익공유 경제성 기초자료
    rev, ps = payload.get("revenue_comparison") or {}, payload.get("profit_sharing") or {}
    ws = _sheet(wb, "이익공유 기초자료", "이익공유 경제성 분석 기초자료(계획서 4.1 마)", common + [
        "절감액은 AI 최적화가 일반 충전 대비 아낀 전력비용(열화비용 차감, 가정값 포함). 배분은 시험용 비율이며 실제 정산이 아니다."])
    _table(ws, ["항목", "값"], [
        ["일반 충전 비용(원)", rev.get("baseline_cost_krw")], ["AI 최적화 비용(원)", rev.get("optimized_cost_krw")],
        ["열화비용(원)", rev.get("degradation_cost_krw")], ["절감액(원)", rev.get("savings_krw")], ["절감률(%)", rev.get("savings_pct")],
    ], "전력비용 비교")
    if ps:
        names = {"owner": "차주", "operator": "충전사업자", "platform": "서비스 운영자"}
        _table(ws, ["주체", "비율(%)", "배분액(원)"],
               [[names.get(k, k), ps["shares_pct"][k], ps["amounts_krw"][k]] for k in ps.get("shares_pct", {})], "배분 시험")

    # 7. 모델링 예외 기록
    mi = payload.get("modeling_issues")
    if mi is not None:
        ws = _sheet(wb, "예외·오류 기록", "모델링 과정의 데이터·오류·예외상황 기록(계획서 4.1 다)", [
            f"생성 시각: {payload.get('generated_at', '')}",
            "같은 단계·같은 원인은 한 줄로 묶고 발생 횟수를 센다. 조치 완료 뒤 다시 생기면 새 줄(재발)로 남는다.",
            "이 표는 대시보드 계산이 아니라 운영 중 실제로 쌓인 기록이다."])
        sm = mi.get("summary") or {}
        _table(ws, ["단계", "미조치(건)", "미조치 발생 횟수", "조치 완료(건)"],
               [[b["label"], b["open"], b["occurrences"], b["resolved"]] for b in (sm.get("by_stage") or {}).values()]
               + [["합계", sm.get("open", 0), sm.get("occurrences", 0), sm.get("resolved", 0)]], "단계별 현황")
        _table(ws, ["번호", "단계", "종류", "내용", "횟수", "처음 발생", "마지막 발생", "상태", "개선 조치", "조치자", "조치 시각"],
               [[x["issue_id"], x["stage_label"], x["kind_label"], x["message"], x["count"], x["first_seen"], x["last_seen"],
                 "조치 완료" if x["status"] == "resolved" else "미조치", x.get("resolution_note"), x.get("resolved_by"), x.get("resolved_at")]
                for x in mi.get("items") or []], "기록 목록")

    # 8. 기술적 한계·개선사항(2차년도 고도화 기준) — 계획서 p.30, 요구사항 E01
    ws = _sheet(wb, "한계·개선 기준", "1차년도 기술적 한계와 2차년도 고도화 기준(계획서 4.1 바)", common + [
        "각 행은 이 결과 파일의 값에서 자동으로 판단한 한계다. 기준을 충족하면 해당 행은 '충족'으로 바뀐다."])
    _table(ws, ["구분", "현재 상태(근거)", "판정", "2차년도 고도화 기준"], limitation_rows(payload), "한계·개선 기준")

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def limitation_rows(payload: dict) -> list[list]:
    """결과 값에서 한계를 판단해 [구분, 현재 상태, 판정, 고도화 기준] 행을 만든다."""
    kpi = payload.get("kpi") or {}
    eff = payload.get("effect_verification") or {}
    rev = payload.get("revenue_comparison") or {}
    prov = kpi.get("data_provenance") or {}
    rows = []
    measured = [k for k, v in prov.items() if v in ("measured", "public")]
    rows.append(["데이터 출처", f"실측·공개 표: {', '.join(measured) or '없음'} / 전체 {len(prov)}표",
                 "충족" if len(measured) == len(prov) and prov else "한계",
                 "EV 텔레메트리·충전 세션·계통 신호 3표 모두 실측/공개 데이터로 전환, 합성 보완분 0건"])
    official = sum(y.get("records", 0) for y in kpi.get("data_official_by_year") or [])
    rows.append(["성과 데이터 건수", f"실측 실적 {official:,}건 (1차 목표 4,000건)", "충족" if official >= 4000 else "한계",
                 "2차년도 4,500건 이상(연차별 미중복), 차량 세션·충전기 세션 산출식 유지"])
    rows.append(["방전(V2G) 실증", f"방전량은 추론값({eff.get('generation', 'inferred')}) — 물리 방전 인프라 없음", "한계",
                 "양방향 충전기·V2G 가능 차량 확보 시 실측 방전량으로 교체, 추론 대비 오차 검증"])
    assumptions = rev.get("assumptions") or {}
    rows.append(["경제성 가정값", f"효율·열화비용 확정 여부 verified={assumptions.get('verified')}",
                 "충족" if assumptions.get("verified") else "한계", "확정 요금표·배터리 열화 근거 반영 후 verified=true"])
    cov = ((eff.get("conditions") or {}).get("coverage")) or {}
    empty = [f"{axis}:{lbl}" for axis, m in cov.items() for lbl, n in m.items() if n == 0]
    rows.append(["운영조건 다양성", f"데이터가 없는 조건 {len(empty)}개" + (f" ({', '.join(empty[:6])})" if empty else ""),
                 "충족" if cov and not empty else "한계", "계절·시간대·SOC·운행유형 전 조건에서 세션 확보(하계·동계 포함)"])
    v2g_rate = eff.get("v2g_prediction_rate_pct")
    rows.append(["V2G 예측률", f"{v2g_rate}% (출처: {eff.get('v2g_prediction_source', '-')})",
                 "충족" if (v2g_rate or 0) >= 80 else "한계", "누적 실측 데이터 재학습, 계획서 성능 목표(MAPE ≤20%, R² ≥0.70) 유지"])
    return rows
