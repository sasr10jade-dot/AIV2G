"""사업수행계획서 v1.0(2026-10-31) 성과지표(KPI) 산식 — 데이터 건수·인프라 규모의 단일 정의.

대시보드·보고서가 각자 다른 산식을 쓰지 않도록 이 모듈 하나에서 계산한다.

계획서 성과지표
- 실증 인프라 : EV 2종 이상 30대, 충전기 2종 이상 150기(2차년도는 1차년도 유지)
- 실증 데이터 : 1차년도 4,000건 이상, 2차년도 4,500건 이상 — 연차별 확보 데이터 미중복
- AI 기반 최적 V2G 예측모델 1건(1차) / 고도화 1건(2차), BM 모델 제안 1건·MOU 1건(2차)은 문서 성과라 여기서 세지 않는다.

데이터 1건의 정의(평가의견 14 반영 문안, 2026-11-20): '필수 필드를 갖추고 품질검증을 통과한 충전 세션 단위 레코드' 1건.
[차량 OBD] EV 30대 x 주 3.5회 x 13주 = 1,365건 + [CPO 거점] 유효 충전 세션 주 250건 x 13주 = 3,250건
(서포터즈 차량 세션 제외) → 합계 4,615건. 서포터즈 이탈 16%를 반영해도 4,397건으로 목표 4,000건 충족(DATA_PLAN).
- 충전기 세션 : 연계 충전기의 유효 충전 세션 1건(충전량 > 0, 시작 시각 기준 연차 배정). 같은 세션이 두 번 들어오면
  1건으로 센다(session_id, 없으면 충전기+시작 시각 기준 중복 제거). 서포터즈 차량(OBD로 이미 세는 차량)의 세션은
  차량 OBD 세션과 겹치므로 CPO 거점 건수에서 뺀다.
- 차량 세션 : 차량 OBD에서 관측된 운행 1회 또는 충전 1회(연속된 시간 구간, 시작 시각 기준 연차 배정)
  — 결과 필드 이름(vehicle_days)은 화면·보고서 호환을 위해 유지하지만 값은 차량 세션 수다(요구사항 K01).
  예전 단위였던 차량-일(활동한 차량·날짜 수)은 `count_vehicle_days`로 참고용으로만 남긴다.
- 전력수요·기상 등 공개 데이터는 참고 건수로만 따로 보여주고 성과 건수에는 넣지 않는다(관리기관 협의 전 보수적 기준).
- 방전은 실측이 아니라 AI 추론이므로(계획서 '수행 전제') 건수에 넣지 않는다.

연차는 사업기간으로 나눈다. 1·2차년도 사이 공백기(2027-04-16~04-30)와 사업기간 밖 데이터는 어느 연차에도 넣지 않는다.
합성(모의) 데이터는 실적이 아니다. 표별 출처(`source.provenance()`)를 받아 실측 표의 건수만 `official_by_year`(성과 실적)로
따로 센다 — EV만 실측이고 충전기는 합성으로 보완 중이면 충전 세션은 실적에 넣지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ---- 계획서 v1.0 목표치 --------------------------------------------------------
TARGET_EV = 30
TARGET_EV_MODELS = 2          # 차종 2종 이상
TARGET_CHARGERS = 150
TARGET_CHARGER_TYPES = 2      # 충전기 2종 이상
TARGET_OPERATORS = 3          # 참여의향서 확보 충전서비스 사업자(이브이시스·플러그링크·나이스인프라)
TARGET_SITES = 3              # 도시형 실증 참여 장소 3곳 이상
ACTIVE_WINDOW_DAYS = 7        # 활동 차량: 마지막 관측 시각 기준 최근 7일 안에 주행·충전·연결이 있었던 차량

# 연차별 사업기간과 데이터 목표(연차별 미중복)
PROJECT_YEARS = {
    "Y1": {"label": "1차년도", "start": date(2026, 11, 1), "end": date(2027, 4, 15), "target": 4000},
    "Y2": {"label": "2차년도", "start": date(2027, 5, 1), "end": date(2027, 11, 30), "target": 4500},
}
TARGET_TOTAL_RECORDS = sum(y["target"] for y in PROJECT_YEARS.values())   # 8,500

# 1차년도 데이터 확보 계획(평가의견 4·14 반영 문안) — 원천별 산출식
DATA_PLAN = {
    "obd": {"label": "차량 OBD 세션", "vehicles": 30, "per_vehicle_week": 3.5, "weeks": 13, "count": 1365},
    "cpo": {"label": "CPO 거점 충전 세션(서포터즈 차량 세션 제외)", "per_week": 250, "weeks": 13, "count": 3250},
    "total": 4615,
    "attrition_pct": 16,
    "total_with_attrition": 4397,   # OBD 1,365 x (1-0.16) + CPO 3,250
    "target": 4000,
}
WEEKLY_PLAN = {"obd": DATA_PLAN["obd"]["count"] / DATA_PLAN["obd"]["weeks"],   # 105건/주
               "cpo": DATA_PLAN["cpo"]["per_week"]}                              # 250건/주
WEEKLY_ALERT_RATIO = 0.90   # 주간 유효 세션이 계획의 90% 미만인 주가 2주 연속이면 대응 조치(평가의견 4)
OBD_ALERT_RATIO = 0.80      # OBD 확보가 계획의 80% 미만인 주가 2주 연속이면 거점 세션 샘플링 확대(평가의견 12)
ALERT_STREAK_WEEKS = 2


def project_year_of(d) -> str | None:
    """날짜가 속한 연차('Y1'/'Y2'). 공백기·사업기간 밖이면 None."""
    if d is None or (isinstance(d, float) and pd.isna(d)):
        return None
    d = pd.Timestamp(d).date()
    for key, y in PROJECT_YEARS.items():
        if y["start"] <= d <= y["end"]:
            return key
    return None


@dataclass(frozen=True)
class YearCount:
    year: str
    label: str
    charger_sessions: int
    vehicle_days: int
    records: int
    target: int
    achieved_pct: float


@dataclass(frozen=True)
class KpiSummary:
    ev_count: int
    ev_model_count: int
    charger_count: int
    charger_type_count: int | None    # 충전기 종류 컬럼이 없으면 None(미집계)
    operator_count: int | None        # 사업자 컬럼이 없으면 None(미집계)
    by_year: list[YearCount]            # 출처와 무관하게 센 연차별 건수(모의 데이터 포함 — 화면 미리보기용)
    official_by_year: list[YearCount]   # 실측 표의 건수만 센 연차별 성과 실적
    out_of_period_records: int        # 공백기·사업기간 밖(어느 연차에도 넣지 않음)
    scenario_records: int             # 기간 구분 없이 센 전체 유효 건수(참고)
    public_reference_records: int     # 공개 데이터 참고 건수(성과 미포함)
    is_real: bool
    provenance: dict = field(default_factory=dict)
    targets: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _supporter_ids(ev_df: pd.DataFrame | None) -> set:
    return set(ev_df["ev_id"].dropna().astype(str)) if ev_df is not None and not ev_df.empty and "ev_id" in ev_df.columns else set()


def session_funnel(charger_df: pd.DataFrame, exclude_ev_ids: set | None = None) -> dict:
    """충전 세션 건수 단계(중복 제거 전·후) — 원천 행 → 유효(충전량>0) → 중복 제거 → 서포터즈 차량 세션 제외(=성과 산입)."""
    if charger_df is None or charger_df.empty or "start_ts" not in charger_df.columns:
        return {"raw": 0, "valid": 0, "deduplicated": 0, "supporter_excluded": 0, "counted": 0, "_rows": pd.DataFrame()}
    df = charger_df.assign(_start=pd.to_datetime(charger_df["start_ts"], errors="coerce")).dropna(subset=["_start"])
    raw = int(len(charger_df))
    energy = pd.to_numeric(df["energy_kwh"], errors="coerce").fillna(0) if "energy_kwh" in df.columns else pd.Series(1, index=df.index)
    # 필수 필드: 충전기 ID·시작 시각·충전량(>0) — 하나라도 없으면 1건으로 세지 않는다
    has_id = df["charger_id"].notna() & (df["charger_id"].astype(str).str.strip() != "") if "charger_id" in df.columns else True
    valid = df[(energy > 0) & has_id]
    key = ["session_id"] if "session_id" in valid.columns and valid["session_id"].notna().all() else \
        [c for c in ("charger_id", "_start") if c in valid.columns]
    dedup = valid.drop_duplicates(subset=key)
    excl = 0
    if exclude_ev_ids and "ev_id" in dedup.columns:
        mask = dedup["ev_id"].astype(str).isin(exclude_ev_ids) & dedup["ev_id"].notna()
        excl = int(mask.sum())
        dedup = dedup[~mask]
    return {"raw": raw, "valid": int(len(valid)), "deduplicated": int(len(dedup)) + excl,
            "supporter_excluded": excl, "counted": int(len(dedup)), "_rows": dedup}


def _session_dates(charger_df: pd.DataFrame, exclude_ev_ids: set | None = None) -> pd.Series:
    rows = session_funnel(charger_df, exclude_ev_ids)["_rows"]
    if rows.empty:
        return pd.Series([], dtype="object")
    return rows["_start"].dt.date.reset_index(drop=True)


def _vehicle_days(ev_df: pd.DataFrame) -> pd.DataFrame:
    """(ev_id, 날짜) 활성 프로파일 목록."""
    if ev_df is None or ev_df.empty:
        return pd.DataFrame(columns=["ev_id", "_day"])
    active = ev_df[
        ev_df["driving"].astype(bool) | ev_df["connected"].astype(bool) | (ev_df["charging_kw"] > 0)
    ]
    if active.empty:
        return pd.DataFrame(columns=["ev_id", "_day"])
    days = active.assign(_day=pd.to_datetime(active["timestamp"]).dt.date)
    return days[["ev_id", "_day"]].drop_duplicates().reset_index(drop=True)


def _vehicle_sessions(ev_df: pd.DataFrame) -> pd.DataFrame:
    """차량 세션 목록(ev_id, 시작 날짜) — 운행(driving) 또는 충전(charging_kw>0) 상태가 이어지는 구간 1개가 1건.
    같은 상태라도 관측 간격이 1시간을 넘으면 새 세션으로 본다."""
    if ev_df is None or ev_df.empty:
        return pd.DataFrame(columns=["ev_id", "_day"])
    df = ev_df[["ev_id", "timestamp", "driving", "charging_kw"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values(["ev_id", "timestamp"])
    driving = df["driving"].fillna(False).astype(bool)
    charging = pd.to_numeric(df["charging_kw"], errors="coerce").fillna(0) > 0
    state = pd.Series(pd.NA, index=df.index, dtype="object")
    state[charging] = "charge"
    state[driving] = "drive"
    prev_state = state.groupby(df["ev_id"]).shift(1)
    gap = df["timestamp"].groupby(df["ev_id"]).diff()
    start = state.notna() & ((prev_state != state) | prev_state.isna() | (gap > pd.Timedelta(hours=1)))
    starts = df[start.fillna(False).astype(bool)]
    return pd.DataFrame({"ev_id": starts["ev_id"].to_numpy(), "_day": starts["timestamp"].dt.date.to_numpy()})


def count_vehicle_sessions(ev_df: pd.DataFrame) -> int:
    """차량 세션 수(운행 1회·충전 1회) — 계획서 p.16 차량 OBD 데이터 산출 단위."""
    return int(len(_vehicle_sessions(ev_df)))


def count_charger_sessions(charger_df: pd.DataFrame, exclude_ev_ids: set | None = None) -> int:
    """유효 충전 세션 수(충전량 0 이하 제외, 중복 제거, exclude_ev_ids 차량의 세션 제외)."""
    return int(len(_session_dates(charger_df, exclude_ev_ids)))


def count_vehicle_days(ev_df: pd.DataFrame) -> int:
    """차량-일: 주행·충전·연결 중 하나라도 관측된 (ev_id, 날짜) 수."""
    return int(len(_vehicle_days(ev_df)))


def count_by_year(ev_df: pd.DataFrame, charger_df: pd.DataFrame,
                  count_sessions: bool = True, count_vehicle_days: bool = True) -> tuple[list[YearCount], int]:
    """연차별 건수와 어느 연차에도 속하지 않는 건수. count_*=False인 표는 0으로 센다(합성 표 제외용)."""
    sess_years = _session_dates(charger_df, _supporter_ids(ev_df)).map(project_year_of)
    vd = _vehicle_sessions(ev_df)
    vd_years = vd["_day"].map(project_year_of) if not vd.empty else pd.Series([], dtype="object")
    rows = []
    for key, y in PROJECT_YEARS.items():
        s = int((sess_years == key).sum()) if count_sessions else 0
        v = int((vd_years == key).sum()) if count_vehicle_days else 0
        rec = s + v
        rows.append(YearCount(key, y["label"], s, v, rec, y["target"], round(rec / y["target"] * 100, 1)))
    out = int(sess_years.isna().sum() + vd_years.isna().sum())
    return rows, out


def plan_targets() -> dict:
    return {
        "ev": TARGET_EV, "ev_models": TARGET_EV_MODELS,
        "chargers": TARGET_CHARGERS, "charger_types": TARGET_CHARGER_TYPES, "operators": TARGET_OPERATORS,
        "sites": TARGET_SITES,
        "records_by_year": {k: y["target"] for k, y in PROJECT_YEARS.items()},
        "records_total": TARGET_TOTAL_RECORDS,
        "data_plan": DATA_PLAN,
    }


def summarize_kpi(
    ev_df: pd.DataFrame,
    charger_df: pd.DataFrame,
    grid_df: pd.DataFrame | None = None,
    is_real: bool = False,
    grid_is_real: bool = False,
    provenance: dict | None = None,
) -> KpiSummary:
    """provenance가 없으면 is_real 하나로 EV·충전기 표 모두의 출처를 정한다(구 호출 호환)."""
    provenance = provenance or {"ev_telemetry": "measured" if is_real else "synthetic",
                                "charger_sessions": "measured" if is_real else "synthetic",
                                "grid_signal": "public" if grid_is_real else "synthetic"}
    by_year, out = count_by_year(ev_df, charger_df)
    official, _ = count_by_year(ev_df, charger_df,
                                count_sessions=provenance.get("charger_sessions") == "measured",
                                count_vehicle_days=provenance.get("ev_telemetry") == "measured")
    grid_is_real = provenance.get("grid_signal") == "public"
    # 공개 데이터 참고 건수: 전력수요·재생에너지·SMP·기상 4종 × 시간단위 행 수, 실데이터일 때만
    public_ref = (len(grid_df) * 4) if (grid_df is not None and grid_is_real) else 0

    def has(df, col):
        return df is not None and col in df.columns

    return KpiSummary(
        ev_count=int(ev_df["ev_id"].nunique()) if not ev_df.empty else 0,
        ev_model_count=int(ev_df["vehicle_model"].nunique()) if has(ev_df, "vehicle_model") else 0,
        charger_count=int(charger_df["charger_id"].nunique()) if has(charger_df, "charger_id") else 0,
        charger_type_count=int(charger_df["charger_type"].nunique()) if has(charger_df, "charger_type") else None,
        operator_count=int(charger_df["operator_id"].nunique()) if has(charger_df, "operator_id") else None,
        by_year=by_year,
        official_by_year=official,
        out_of_period_records=out,
        scenario_records=count_charger_sessions(charger_df, _supporter_ids(ev_df)) + count_vehicle_sessions(ev_df),
        public_reference_records=public_ref,
        is_real=provenance.get("ev_telemetry") == "measured",
        provenance=provenance,
        targets=plan_targets(),
    )


def load_site_registry() -> dict:
    """참여 장소 등록부(config/sites.json) — site_id → 이름·유형·주소·확정 여부."""
    import json
    from src.paths import PROJECT_ROOT
    path = PROJECT_ROOT / "config" / "sites.json"
    if not path.exists():
        return {}
    return {s["site_id"]: s for s in json.loads(path.read_text(encoding="utf-8")).get("sites", [])}


def charger_status_list(charger_df: pd.DataFrame, site_registry: dict | None = None, operator_names: dict | None = None,
                        inactive_hours: float = 168.0) -> list[dict]:
    """충전기별 현재 상태 — 대시보드 '충전기 관제 리스트'의 근거(세션 데이터에서 계산, 고정 목록 아님).

    기준 시각 = 데이터의 마지막 세션 종료 시각. 그 시각에 진행 중인 세션이 있으면 '충전중'(평균 출력),
    최근 inactive_hours(7일) 안에 세션이 있으면 '대기(사용 가능)', 없으면 'inactive'(장기 미사용 — 통신 장애와 구분).
    """
    if charger_df is None or charger_df.empty or "charger_id" not in charger_df.columns:
        return []
    site_registry = site_registry or {}
    operator_names = operator_names or {}
    df = charger_df.copy()
    df["start_ts"] = pd.to_datetime(df["start_ts"], errors="coerce")
    if "end_ts" in df.columns:
        df["end_ts"] = pd.to_datetime(df["end_ts"], errors="coerce")
    else:
        df["end_ts"] = df["start_ts"] + pd.to_timedelta(pd.to_numeric(df.get("duration_h", 0), errors="coerce").fillna(0), unit="h")
    df["end_ts"] = df["end_ts"].fillna(df["start_ts"])
    now = df["end_ts"].max()
    day0 = now.normalize()
    out = []
    for cid, g in df.groupby("charger_id"):
        g = g.sort_values("start_ts")
        last = g.iloc[-1]
        active = g[(g["start_ts"] <= now) & (g["end_ts"] >= now)]
        site = site_registry.get(last.get("site_id"), {})
        ctype = str(last.get("charger_type") or "")
        if not active.empty:
            status, current_kw = "charging", round(float(active.iloc[-1].get("avg_power_kw") or 0.0), 1)
        elif (now - last["end_ts"]).total_seconds() <= inactive_hours * 3600:
            status, current_kw = "idle", 0.0
        else:
            status, current_kw = "inactive", 0.0
        op = str(last.get("operator_id") or "-")
        out.append({
            "charger_id": cid,
            "operator_id": op,
            "operator_name": operator_names.get(op, op),
            "site_id": last.get("site_id"),
            "site_name": site.get("name"),
            "site_address": site.get("address"),
            "charger_type": ctype,
            "is_fast": ctype.upper().startswith("DC"),
            "max_kw": round(float(pd.to_numeric(g.get("avg_power_kw"), errors="coerce").max() or 0.0), 1),
            "connector_type": last.get("connector_type"),
            "status": status,
            "current_kw": current_kw,
            "energy_last_day_kwh": round(float(pd.to_numeric(g.loc[g["start_ts"] >= day0, "energy_kwh"], errors="coerce").sum()), 1),
            "sessions": int(len(g)),
            "last_session_end": last["end_ts"].isoformat() if pd.notna(last["end_ts"]) else None,
        })
    return out


def infra_status(ev_df: pd.DataFrame, charger_df: pd.DataFrame, site_registry: dict | None = None) -> dict:
    """관리자 현황판(계획서 4.1 가: 서포터즈·차량, 충전소·충전기, 데이터 현황) — 참여 장소·사업자별 충전기·활동 차량.
    장소·사업자 컬럼이 없는 데이터면 해당 목록은 비워 두고 count를 None으로 둔다(미집계)."""
    def has(df, col):
        return df is not None and not df.empty and col in df.columns

    def group(col: str, key: str) -> list[dict]:
        if not has(charger_df, col):
            return []
        df = charger_df.assign(_k=charger_df[col].fillna("미지정"))
        energy = pd.to_numeric(df["energy_kwh"], errors="coerce").fillna(0) if "energy_kwh" in df.columns else None
        out = []
        for k, g in df.groupby("_k", sort=True):
            row = {key: str(k), "chargers": int(g["charger_id"].nunique()),
                   "sessions": int((energy.loc[g.index] > 0).sum()) if energy is not None else int(len(g))}
            if "charger_type" in g.columns:
                row["charger_types"] = {str(t): int(n) for t, n in
                                        g.drop_duplicates("charger_id")["charger_type"].fillna("미지정").value_counts().sort_index().items()}
            if key == "site_id" and "operator_id" in g.columns:
                row["operators"] = sorted(str(o) for o in g["operator_id"].dropna().unique())
            out.append(row)
        return out

    sites = group("site_id", "site_id")
    for row in sites:
        meta = (site_registry or {}).get(row["site_id"], {})
        row.update({k: meta.get(k) for k in ("name", "name_en", "type", "address", "address_en", "confirmed")})
    operators = group("operator_id", "operator_id")

    vehicles = {"total": 0, "active": 0, "active_window_days": ACTIVE_WINDOW_DAYS, "as_of": None, "by_model": {}}
    if has(ev_df, "ev_id"):
        vehicles["total"] = int(ev_df["ev_id"].nunique())
        if "vehicle_model" in ev_df.columns:
            vehicles["by_model"] = {str(m): int(n) for m, n in
                                    ev_df.drop_duplicates("ev_id")["vehicle_model"].fillna("미지정").value_counts().sort_index().items()}
        vd = _vehicle_days(ev_df)
        if not vd.empty:
            last = max(vd["_day"])
            since = last - timedelta(days=ACTIVE_WINDOW_DAYS - 1)
            vehicles["active"] = int(vd.loc[vd["_day"] >= since, "ev_id"].nunique())
            vehicles["as_of"] = last.isoformat()

    return {
        "sites": sites,
        "site_count": len([s for s in sites if s["site_id"] != "미지정"]) if sites else None,
        "operators": operators,
        "vehicles": vehicles,
        "targets": {"sites": TARGET_SITES, "operators": TARGET_OPERATORS, "ev": TARGET_EV, "chargers": TARGET_CHARGERS},
    }


PACE_WINDOW_DAYS = 14     # 수집 속도: 마지막 관측일 기준 최근 14일 평균
ROSTER_WINDOW_DAYS = 28   # 차량별 현황: 최근 4주


def collection_status(ev_df: pd.DataFrame, charger_df: pd.DataFrame, count_sessions: bool = True,
                      count_vehicle_days: bool = True, days: int = 60) -> dict:
    """데이터 현황판(계획서 리스크 대응: '데이터 현황판으로 목표 대비 수집 속도 관리') — 일자별 유효 건수,
    현재 속도로 연차 종료일까지 수집했을 때의 예상 건수, 경고, 차량별 최근 4주 현황.
    count_*=False인 표(합성)는 0으로 세어 성과 실적과 같은 기준을 쓴다."""
    sess = _session_dates(charger_df, _supporter_ids(ev_df)) if count_sessions else pd.Series([], dtype="object")
    vd = _vehicle_sessions(ev_df) if count_vehicle_days else pd.DataFrame(columns=["ev_id", "_day"])
    s_daily = sess.value_counts() if len(sess) else pd.Series(dtype=int)
    v_daily = vd["_day"].value_counts() if not vd.empty else pd.Series(dtype=int)
    all_days = sorted(set(s_daily.index) | set(v_daily.index))
    out: dict = {"daily": [], "pace": None, "warnings": [], "roster": []}
    if all_days:
        last = all_days[-1]
        span = [last - timedelta(days=i) for i in range(days - 1, -1, -1)]
        out["daily"] = [{"date": d.isoformat(), "charger_sessions": int(s_daily.get(d, 0)),
                         "vehicle_days": int(v_daily.get(d, 0))} for d in span]
        recent = [r["charger_sessions"] + r["vehicle_days"] for r in out["daily"][-PACE_WINDOW_DAYS:]]
        per_day = sum(recent) / len(recent)
        key = project_year_of(last) or next((k for k, y in PROJECT_YEARS.items() if last < y["start"]), None)
        if key:
            y = PROJECT_YEARS[key]
            counted = int((sess.map(project_year_of) == key).sum()) + (int((vd["_day"].map(project_year_of) == key).sum()) if not vd.empty else 0)
            remaining = max(0, (y["end"] - max(last, y["start"] - timedelta(days=1))).days)
            expected = int(round(counted + per_day * remaining))
            out["pace"] = {"year": key, "label": y["label"], "as_of": last.isoformat(), "per_day": round(per_day, 1),
                           "counted": counted, "remaining_days": remaining, "expected": expected, "target": y["target"],
                           "expected_pct": round(expected / y["target"] * 100, 1),
                           "per_day_needed": round(max(0, y["target"] - counted) / remaining, 1) if remaining else None}
            if expected < y["target"]:
                out["warnings"].append({"code": "pace_short", "per_day": round(per_day), "label": y["label"],
                                        "expected": expected, "target": y["target"]})

    # 차량별 최근 4주 현황(출처와 무관하게 — 운영 점검용)
    if ev_df is not None and not ev_df.empty and "ev_id" in ev_df.columns:
        df = ev_df.assign(_day=pd.to_datetime(ev_df["timestamp"]).dt.date)
        last = df["_day"].max()
        since = last - timedelta(days=ROSTER_WINDOW_DAYS - 1)
        win = df[df["_day"] >= since]
        active_since = last - timedelta(days=ACTIVE_WINDOW_DAYS - 1)
        act = _vehicle_days(ev_df)
        last_seen = act.groupby("ev_id")["_day"].max() if not act.empty else pd.Series(dtype="object")
        for ev_id, g in df.groupby("ev_id", sort=True):
            w = win[win["ev_id"] == ev_id]
            charge_days = int(w.loc[w["charging_kw"] > 0, "_day"].nunique()) if not w.empty else 0
            seen = last_seen.get(ev_id)
            soc = g.sort_values("timestamp")["soc_pct"].iloc[-1] if "soc_pct" in g.columns else None
            out["roster"].append({
                "ev_id": str(ev_id),
                "vehicle_model": str(g["vehicle_model"].iloc[0]) if "vehicle_model" in g.columns else None,
                "active": bool(seen is not None and seen >= active_since),
                "last_seen": seen.isoformat() if seen is not None else None,
                "charge_days_4w": charge_days,
                "charge_per_week": round(charge_days / (ROSTER_WINDOW_DAYS / 7), 1),
                "last_soc_pct": None if soc is None or pd.isna(soc) else round(float(soc), 1),
            })
        n_active = sum(r["active"] for r in out["roster"])
        if n_active < TARGET_EV:
            out["warnings"].append({"code": "few_active", "active": n_active, "target": TARGET_EV})
    out["weekly"] = weekly_monitor(ev_df, charger_df, count_sessions, count_vehicle_days)
    out["warnings"].extend(out["weekly"]["alerts"])
    f = session_funnel(charger_df, _supporter_ids(ev_df)) if count_sessions else None
    out["funnel"] = {k: v for k, v in f.items() if not k.startswith("_")} if f else None
    out["data_plan"] = DATA_PLAN
    out["milestones"] = load_milestones()
    return out


MILESTONES_FILE = Path(__file__).resolve().parents[1] / "config" / "data_plan.json"


def load_milestones(path: Path | None = None) -> dict:
    """일정 마일스톤(평가의견 4) — config/data_plan.json. 날짜가 지났는지(done)를 오늘 기준으로 붙인다."""
    path = path or MILESTONES_FILE
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"items": [], "verified": False}
    today = date.today()
    items = [{**m, "done": date.fromisoformat(m["date"]) <= today} for m in cfg.get("milestones", [])]
    return {"items": items, "verified": bool(cfg.get("verified")), "note": cfg.get("note")}


def weekly_monitor(ev_df: pd.DataFrame, charger_df: pd.DataFrame, count_sessions: bool = True,
                   count_vehicles: bool = True, weeks: int = 13) -> dict:
    """주간 점검 지표(평가의견 4) — 주별 활동 차량 수, 주간 유효 세션(OBD·CPO), 누적 대 계획.
    경고: 주간 유효 세션이 계획의 90% 미만인 주가 2주 연속(대응 조치), OBD가 계획의 80% 미만 2주 연속(거점 세션 샘플링 확대)."""
    vs = _vehicle_sessions(ev_df) if count_vehicles else pd.DataFrame(columns=["ev_id", "_day"])
    cs = _session_dates(charger_df, _supporter_ids(ev_df)) if count_sessions else pd.Series([], dtype="object")
    act = _vehicle_days(ev_df)

    def week_of(days) -> pd.Series:
        d = pd.to_datetime(pd.Series(list(days)))
        return (d - pd.to_timedelta(d.dt.dayofweek, unit="D")).dt.date

    obd_w = week_of(vs["_day"]).value_counts() if not vs.empty else pd.Series(dtype=int)
    cpo_w = week_of(cs).value_counts() if len(cs) else pd.Series(dtype=int)
    act_w = act.assign(_w=week_of(act["_day"]).to_numpy()).groupby("_w")["ev_id"].nunique() if not act.empty else pd.Series(dtype=int)
    all_w = sorted(set(obd_w.index) | set(cpo_w.index))
    plan_obd, plan_cpo = WEEKLY_PLAN["obd"], WEEKLY_PLAN["cpo"]
    plan_total = plan_obd + plan_cpo
    rows = []
    if all_w:
        last = all_w[-1]
        span = [last - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
        span = [w for w in span if w >= all_w[0]]
        cum = cum_plan = 0
        for w in span:
            o, c = int(obd_w.get(w, 0)), int(cpo_w.get(w, 0))
            cum += o + c
            cum_plan += plan_total
            rows.append({"week_start": w.isoformat(), "active_vehicles": int(act_w.get(w, 0)), "obd": o, "cpo": c, "total": o + c,
                         "plan": round(plan_total), "ratio_pct": round((o + c) / plan_total * 100, 1),
                         "obd_ratio_pct": round(o / plan_obd * 100, 1),
                         "cumulative": cum, "cumulative_plan": round(cum_plan), "cumulative_pct": round(cum / cum_plan * 100, 1)})

    def streak(cond) -> int:
        n = 0
        for r in reversed(rows):
            if not cond(r):
                break
            n += 1
        return n

    alerts = []
    s90 = streak(lambda r: r["ratio_pct"] < WEEKLY_ALERT_RATIO * 100)
    if s90 >= ALERT_STREAK_WEEKS:
        alerts.append({"code": "weekly_below_90", "weeks": s90,
                       "text": f"주간 유효 세션이 계획(주 {round(plan_total)}건)의 90% 미만 {s90}주 연속 — 대응 조치 필요(모집 독려·예비 서포터즈 투입·거점 세션 확대)"})
    s80 = streak(lambda r: r["obd_ratio_pct"] < OBD_ALERT_RATIO * 100) if count_vehicles else 0
    if s80 >= ALERT_STREAK_WEEKS:
        alerts.append({"code": "obd_below_80", "weeks": s80,
                       "text": f"차량 OBD 세션이 계획(주 {round(plan_obd)}건)의 80% 미만 {s80}주 연속 — 거점 충전 세션 샘플링을 확대해 4,000건 유지"})
    return {"rows": rows, "plan": {"obd": round(plan_obd), "cpo": plan_cpo, "total": round(plan_total)},
            "thresholds": {"weekly_pct": WEEKLY_ALERT_RATIO * 100, "obd_pct": OBD_ALERT_RATIO * 100, "streak_weeks": ALERT_STREAK_WEEKS},
            "alerts": alerts}
