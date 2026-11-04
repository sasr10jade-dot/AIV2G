"""사업수행계획서 v1.0(2026-10-31) 성과지표(KPI) 산식 — 데이터 건수·인프라 규모의 단일 정의.

대시보드·보고서가 각자 다른 산식을 쓰지 않도록 이 모듈 하나에서 계산한다.

계획서 성과지표
- 실증 인프라 : EV 2종 이상 30대, 충전기 2종 이상 150기(2차년도는 1차년도 유지)
- 실증 데이터 : 1차년도 4,000건 이상, 2차년도 4,500건 이상 — 연차별 확보 데이터 미중복
- AI 기반 최적 V2G 예측모델 1건(1차) / 고도화 1건(2차), BM 모델 제안 1건·MOU 1건(2차)은 문서 성과라 여기서 세지 않는다.

데이터 1건의 정의(계획서 "수집·정제된 유효 데이터의 최종 누적 건수")
- 충전 세션 : 연계 충전기의 유효 충전 세션 1건(충전량 > 0, 시작 시각 기준 연차 배정)
- 차량-일 프로파일 : 그 날짜에 주행·충전·연결 중 하나라도 관측된 (ev_id, 날짜) 1건
- 전력수요·기상 등 공개 데이터는 참고 건수로만 따로 보여주고 성과 건수에는 넣지 않는다(관리기관 협의 전 보수적 기준).
- 방전은 실측이 아니라 AI 추론이므로(계획서 '수행 전제') 건수에 넣지 않는다.

연차는 사업기간으로 나눈다. 1·2차년도 사이 공백기(2027-04-16~04-30)와 사업기간 밖 데이터는 어느 연차에도 넣지 않는다.
합성(모의) 데이터는 실적이 아니다. 표별 출처(`source.provenance()`)를 받아 실측 표의 건수만 `official_by_year`(성과 실적)로
따로 센다 — EV만 실측이고 충전기는 합성으로 보완 중이면 충전 세션은 실적에 넣지 않는다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

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


def _session_dates(charger_df: pd.DataFrame) -> pd.Series:
    if charger_df is None or charger_df.empty or "start_ts" not in charger_df.columns:
        return pd.Series([], dtype="object")
    energy = pd.to_numeric(charger_df["energy_kwh"], errors="coerce").fillna(0) if "energy_kwh" in charger_df.columns else 1
    valid = charger_df[energy > 0]
    return pd.to_datetime(valid["start_ts"], errors="coerce").dropna().dt.date.reset_index(drop=True)


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


def count_charger_sessions(charger_df: pd.DataFrame) -> int:
    """유효 충전 세션 수(충전량 0 이하 제외)."""
    return int(len(_session_dates(charger_df)))


def count_vehicle_days(ev_df: pd.DataFrame) -> int:
    """차량-일: 주행·충전·연결 중 하나라도 관측된 (ev_id, 날짜) 수."""
    return int(len(_vehicle_days(ev_df)))


def count_by_year(ev_df: pd.DataFrame, charger_df: pd.DataFrame,
                  count_sessions: bool = True, count_vehicle_days: bool = True) -> tuple[list[YearCount], int]:
    """연차별 건수와 어느 연차에도 속하지 않는 건수. count_*=False인 표는 0으로 센다(합성 표 제외용)."""
    sess_years = _session_dates(charger_df).map(project_year_of)
    vd = _vehicle_days(ev_df)
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
        scenario_records=count_charger_sessions(charger_df) + count_vehicle_days(ev_df),
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
    sess = _session_dates(charger_df) if count_sessions else pd.Series([], dtype="object")
    vd = _vehicle_days(ev_df) if count_vehicle_days else pd.DataFrame(columns=["ev_id", "_day"])
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
    return out
