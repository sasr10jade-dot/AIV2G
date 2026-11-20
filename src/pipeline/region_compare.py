"""타 지역 비교 데이터(대조군) — 요구사항 Y2E01(계획서 p.33), 평가의견 9 반영 문안.

실증 지역(광주·전남)과 대조 지역(config/comparison_regions.json: 제주·수도권 경기)의 시간 단위 공개 데이터
(전력수요·재생에너지 발전·기상)를 모아 지역 특성 비교표를 만들고, 실증 지역 데이터로 학습한 수요 패턴 모델을
대조 지역 데이터에 적용한 결과(예측 정확도 = (1 − WAPE) × 100)를 2차년도 확산 모델의 보정 근거로 낸다.

- 기온: `sync_region_temperature` (기상청 ASOS) → data/raw/regions/<id>_temperature.csv
- 전력수요·재생에너지: `save_region_series(region_id, column, series)` — 공공데이터 지역별 CSV를 지역 이름으로 걸러 저장
  → data/raw/regions/<id>_grid.csv (power_demand_mw, renewable_gen_mw)
- 예측 적용: `transfer_test(grid_df)` — 실증 지역 grid_signal로 (시각·요일·월·기온 → 정규화 전력수요) 모델을 학습해
  대조 지역에 적용. 지역 규모가 달라 수요를 각 지역 평균으로 나눈 '모양'을 비교한다.

- 수집: `sync_region_temperature(region_id, start, end)` → data/raw/regions/<id>_temperature.csv (기간이 겹치면 최신 값으로 갱신)
- 실증 지역은 grid_signal의 temperature_c(실측 반영분 또는 합성)를 쓴다.
- 비교: 평균·최저·최고 기온, 영하 시간 비율(저온 시 충전 효율 저하), 30℃ 이상 시간 비율(고온 시 냉방 부하), 수집 일수.
- 수집 전인 지역은 '수집 전'으로 표시하고 값을 지어내지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.paths import PROJECT_ROOT, RAW_DIR

CONFIG = PROJECT_ROOT / "config" / "comparison_regions.json"
REGION_DIR = RAW_DIR / "regions"


def load_regions(path: Path | None = None) -> dict:
    return json.loads(Path(path or CONFIG).read_text(encoding="utf-8"))


def _region(region_id: str) -> dict:
    cfg = load_regions()
    for r in [cfg["pilot"], *cfg["controls"]]:
        if r["id"] == region_id:
            return r
    raise KeyError(region_id)


def region_file(region_id: str) -> Path:
    return REGION_DIR / f"{region_id}_temperature.csv"


def region_grid_file(region_id: str) -> Path:
    return REGION_DIR / f"{region_id}_grid.csv"


SERIES_COLUMNS = ("power_demand_mw", "renewable_gen_mw")


def save_region_series(region_id: str, column: str, series: pd.DataFrame) -> int:
    """대조 지역의 시간별 전력수요·재생에너지 발전량 저장(같은 시각은 새 값으로). series: timestamp, value."""
    _region(region_id)
    if column not in SERIES_COLUMNS:
        raise ValueError(f"column은 {SERIES_COLUMNS} 중 하나입니다")
    REGION_DIR.mkdir(parents=True, exist_ok=True)
    f = region_grid_file(region_id)
    new = pd.DataFrame({"timestamp": pd.to_datetime(series["timestamp"]), column: pd.to_numeric(series["value"], errors="coerce")})
    if f.exists():
        old = pd.read_csv(f, parse_dates=["timestamp"])
        new = _merge_col(old, new, column)
    new = new.sort_values("timestamp").reset_index(drop=True)
    new.to_csv(f, index=False)
    return int(new[column].notna().sum())


def _merge_col(old: pd.DataFrame, new: pd.DataFrame, column: str) -> pd.DataFrame:
    out = old.set_index("timestamp")
    upd = new.set_index("timestamp")[column]
    out = out.reindex(out.index.union(upd.index))
    out.loc[upd.index, column] = upd
    return out.rename_axis("timestamp").reset_index()


def _region_frame(region_id: str) -> pd.DataFrame | None:
    """대조 지역의 시간별 표(기온 + 전력수요·재생에너지) — 수집된 것만."""
    parts = []
    for f in (region_file(region_id), region_grid_file(region_id)):
        if f.exists():
            parts.append(pd.read_csv(f, parse_dates=["timestamp"]).set_index("timestamp"))
    if not parts:
        return None
    return pd.concat(parts, axis=1).groupby(level=0).last().reset_index()


def _grid_stats(df: pd.DataFrame) -> dict:
    out = {}
    if "power_demand_mw" in df.columns and df["power_demand_mw"].notna().any():
        d = df.dropna(subset=["power_demand_mw"])
        prof = d.groupby(d["timestamp"].dt.hour)["power_demand_mw"].mean()
        out.update({"demand_mean_mw": round(float(d["power_demand_mw"].mean()), 1),
                    "demand_peak_hour": int(prof.idxmax()), "demand_hours": int(len(d)),
                    "demand_load_factor_pct": round(float(d["power_demand_mw"].mean() / d["power_demand_mw"].max() * 100), 1)})
    if "renewable_gen_mw" in df.columns and df["renewable_gen_mw"].notna().any():
        r = df.dropna(subset=["renewable_gen_mw"])
        out["renewable_mean_mw"] = round(float(r["renewable_gen_mw"].mean()), 1)
        if "power_demand_mw" in df.columns:
            both = df.dropna(subset=["renewable_gen_mw", "power_demand_mw"])
            if len(both) and both["power_demand_mw"].sum() > 0:
                out["renewable_share_pct"] = round(float(both["renewable_gen_mw"].sum() / both["power_demand_mw"].sum() * 100), 1)
                # 재생에너지가 수요의 일정 비율(30%) 이상인 시간 — 출력제어 위험 시간대 참고
                out["high_renewable_hours_pct"] = round(float((both["renewable_gen_mw"] >= 0.3 * both["power_demand_mw"]).mean() * 100), 1)
    return out


def _features(df: pd.DataFrame) -> pd.DataFrame:
    ts = pd.to_datetime(df["timestamp"])
    X = pd.DataFrame({"hour": ts.dt.hour, "dow": ts.dt.dayofweek, "month": ts.dt.month, "weekend": (ts.dt.dayofweek >= 5).astype(int)})
    if "temperature_c" in df.columns:
        X["temp"] = pd.to_numeric(df["temperature_c"], errors="coerce").to_numpy()
    return X


def transfer_test(grid_df: pd.DataFrame) -> list[dict]:
    """실증 지역 수요 패턴 모델을 대조 지역에 적용 — 정규화 수요(지역 평균=1) 예측 정확도 = (1 − WAPE) × 100."""
    import lightgbm as lgb
    from src.model.demand_forecast import wape
    pilot = grid_df.dropna(subset=["power_demand_mw"]).copy()
    if pilot.empty or pilot["power_demand_mw"].mean() <= 0:
        return []
    y = pilot["power_demand_mw"] / pilot["power_demand_mw"].mean()
    X = _features(pilot)
    model = lgb.LGBMRegressor(n_estimators=200, learning_rate=0.05, num_leaves=15, random_state=42, verbosity=-1).fit(X, y)
    out = []
    for r in load_regions()["controls"]:
        df = _region_frame(r["id"])
        if df is None or "power_demand_mw" not in df.columns or df["power_demand_mw"].dropna().empty:
            out.append({"id": r["id"], "label": r["label"], "status": "수집 전"})
            continue
        d = df.dropna(subset=["power_demand_mw"])
        if "temperature_c" not in d.columns and "temp" in X.columns:
            d = d.assign(temperature_c=float("nan"))
        yt = d["power_demand_mw"] / d["power_demand_mw"].mean()
        Xr = _features(d)[X.columns]
        pred = model.predict(Xr)
        w = wape(yt.to_numpy(), pred)
        out.append({"id": r["id"], "label": r["label"], "status": "적용", "hours": int(len(d)),
                    "accuracy_pct": round(max(0.0, 100 - w), 1), "wape_pct": round(w, 1)})
    return out


def save_temperature(region_id: str, df: pd.DataFrame) -> int:
    """기존 파일과 합치되 같은 시각은 새 값으로 바꾼다."""
    REGION_DIR.mkdir(parents=True, exist_ok=True)
    f = region_file(region_id)
    new = df[["timestamp", "temperature_c"]].copy()
    new["timestamp"] = pd.to_datetime(new["timestamp"])
    if f.exists():
        old = pd.read_csv(f, parse_dates=["timestamp"])
        new = pd.concat([old, new]).drop_duplicates("timestamp", keep="last")
    new = new.sort_values("timestamp").reset_index(drop=True)
    new.to_csv(f, index=False)
    return len(new)


def sync_region_temperature(region_id: str, start, end, fetch=None) -> dict:
    """기상청 ASOS로 해당 지역 관측소 기온을 받아 저장한다. fetch는 테스트용 주입(기본: weather_holiday.fetch_hourly_temperature)."""
    region = _region(region_id)
    if fetch is None:
        from src.pipeline.weather_holiday import fetch_hourly_temperature as fetch
    df = fetch(start, end, station=region["asos_station"])
    total = save_temperature(region_id, df)
    return {"region": region_id, "station": region["asos_station"], "fetched_hours": len(df), "stored_hours": total}


def _stats(temp: pd.Series, ts: pd.Series) -> dict:
    t = pd.to_numeric(temp, errors="coerce").dropna()
    if t.empty:
        return {"status": "수집 전"}
    return {
        "status": "수집됨",
        "hours": int(len(t)),
        "days": int(pd.to_datetime(ts).dt.normalize().nunique()),
        "from": pd.to_datetime(ts).min().strftime("%Y-%m-%d"),
        "to": pd.to_datetime(ts).max().strftime("%Y-%m-%d"),
        "mean_c": round(float(t.mean()), 1),
        "min_c": round(float(t.min()), 1),
        "max_c": round(float(t.max()), 1),
        "below_zero_pct": round(float((t < 0).mean() * 100), 1),
        "above_30_pct": round(float((t >= 30).mean() * 100), 1),
    }


def compare(grid_df: pd.DataFrame, pilot_label_source: str) -> dict:
    """실증 지역(grid_signal 기온)과 대조 지역(수집 파일)의 기상 조건 비교표."""
    cfg = load_regions()
    rows = [{"id": cfg["pilot"]["id"], "label": cfg["pilot"]["label"], "role": "실증", "station": cfg["pilot"]["asos_station"],
             "source": pilot_label_source, **_stats(grid_df["temperature_c"], grid_df["timestamp"])}]
    for r in cfg["controls"]:
        f = region_file(r["id"])
        if f.exists():
            df = pd.read_csv(f, parse_dates=["timestamp"])
            st = _stats(df["temperature_c"], df["timestamp"])
            src = "기상청 ASOS(실측)"
        else:
            st, src = {"status": "수집 전"}, "-"
        gdf = _region_frame(r["id"])
        rows.append({"id": r["id"], "label": r["label"], "role": "대조군", "station": r["asos_station"], "source": src, **st,
                     **(_grid_stats(gdf) if gdf is not None else {})})
    rows[0].update(_grid_stats(grid_df.assign(timestamp=pd.to_datetime(grid_df["timestamp"]))))
    pilot = rows[0]
    for row in rows[1:]:
        if row.get("status") == "수집됨" and pilot.get("status") == "수집됨":
            row["mean_diff_c"] = round(row["mean_c"] - pilot["mean_c"], 1)
    try:
        transfer = transfer_test(grid_df.assign(timestamp=pd.to_datetime(grid_df["timestamp"])))
    except Exception as exc:   # 모델 학습 실패가 비교표 전체를 막지 않게
        transfer = [{"status": f"적용 실패: {type(exc).__name__}"}]
    return {"regions": rows, "transfer": transfer, "verified": bool(cfg.get("verified")),
            "variables": ["전력수요(MW)", "재생에너지 발전(MW)", "기온(℃)"],
            "note": "대조 지역(제주·수도권 경기)은 평가의견 9 반영 문안 기준이며 관리기관 협의 전 예시입니다. "
                    "실증 지역 데이터가 합성이면 비교·예측 적용 결과도 참고용입니다. 예측 적용은 지역 평균으로 정규화한 수요 패턴 기준."}
