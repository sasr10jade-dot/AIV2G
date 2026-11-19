"""타 지역 비교 데이터(대조군) — 요구사항 Y2E01(계획서 p.33).

실증 지역(광주·나주)과 대조 지역(config/comparison_regions.json)의 시간별 기온을 같은 기상청 ASOS API로 모아
EV 충전·배터리 효율에 영향을 주는 기상 조건을 나란히 비교한다.

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
        rows.append({"id": r["id"], "label": r["label"], "role": "대조군", "station": r["asos_station"], "source": src, **st})
    pilot = rows[0]
    for row in rows[1:]:
        if row.get("status") == "수집됨" and pilot.get("status") == "수집됨":
            row["mean_diff_c"] = round(row["mean_c"] - pilot["mean_c"], 1)
    return {"regions": rows, "verified": bool(cfg.get("verified")),
            "note": "대조 지역 선정은 관리기관 협의 전 예시입니다. 실증 지역 기온이 합성이면 비교도 참고용입니다."}
