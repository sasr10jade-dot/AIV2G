"""㈜아크릴 예측모델 연계 — 계획서 5.2 '기관 간 연계(인터페이스) 및 책임'의 코드화.

  아우토크립트 → 아크릴  학습용 정제 데이터(가명처리) 제공. 1차 학습용 데이터는 2027-01 초 누적분 제공 목표.
  아크릴 → 아우토크립트  예측 추론 결과(충전수요·전력수요·EV 가용자원) 일 배치 제공 → V2G 의사결정 입력.

데이터 제공(export)
- 차량 ID는 가명(`pseudonym.pseudo_id("ev", …)`)으로 바꾸고, 번호판 등 식별정보 컬럼은 싣지 않는다.
- 외부 제공 동의(share_external)를 명시적으로 철회한 차량은 뺀다.
- 행마다 data_origin(measured=실측 · public=공개 데이터 · synthetic=합성)을 붙이고, 파일마다 SHA-256을 manifest에 남긴다.
- 제공 이력은 curated.db `dataset_exports`에 남는다(언제·누가·어느 기간·몇 건).

예측 결과 수신(forecast batch)
- 한 배치 = 한 모델 버전이 한 번 낸 예측 묶음. 같은 batch_id를 다시 보내면 무시한다(재전송 안전).
- 값은 항상 추론값이다(generation=inferred). 실측과 같은 표에 섞지 않고 `external_forecasts`에 따로 둔다.
- 같은 시각에 배치가 여러 개면 가장 늦게 발행된(issued_at) 배치 값을 쓴다.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.datagen.config import SimConfig
from src.paths import DATA_DIR
from src.pipeline import store
from src.pipeline.source import load_scenario
from src.pipeline.store import build_city_hourly
from src.privacy import consent
from src.privacy.pseudonym import pseudo_id

EXPORT_DIR = DATA_DIR / "exports"
PROVIDER = "acryl"
FORECAST_TARGETS = {
    "ev_charging_demand_kw": "EV 충전수요(kW)",
    "power_demand_mw": "시간대별 전력수요(MW)",
    "ev_available_v2g_kwh": "EV 가용 배터리 자원(kWh)",
}
MAX_POINTS_PER_BATCH = 5000
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 학습 데이터로 내보내는 컬럼(이 밖의 컬럼은 싣지 않는다 — 식별정보가 새로 생겨도 자동으로 빠진다)
EXPORT_COLUMNS = {
    "ev_telemetry": ["timestamp", "ev_id", "ev_type", "vehicle_model", "zone", "soc_pct", "connected", "driving",
                     "charging_kw", "battery_capacity_kwh", "v2g_capable"],
    "charger_sessions": ["session_id", "charger_id", "zone", "start_ts", "end_ts", "duration_h", "energy_kwh", "ev_id",
                         "operator_id", "charger_type", "connector_type", "avg_power_kw", "stop_reason", "error_code"],
    "grid_signal": ["timestamp", "power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh", "is_holiday"],
    "city_hourly": ["timestamp", "power_demand_mw", "renewable_gen_mw", "temperature_c", "price_krw_per_kwh", "is_holiday",
                    "ev_charging_demand_kw", "ev_available_v2g_kwh", "ev_connected_count"],
}


class LinkError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ 학습 데이터 제공 --

def _share_withdrawn(ev_ids) -> set[str]:
    """외부 제공 동의를 받았다가 철회해 지금 유효한 동의가 없는 차량."""
    rows = consent.list_consents()
    by_ev: dict[str, list] = {}
    for r in rows:
        if r["purpose"] == "share_external":
            by_ev.setdefault(r["ev_id"], []).append(r)
    return {ev for ev in ev_ids if ev in by_ev and all(r["withdrawn_at"] for r in by_ev[ev])}


def _clip_period(df: pd.DataFrame, col: str, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    ts = pd.to_datetime(df[col])
    keep = pd.Series(True, index=df.index)
    if start is not None:
        keep &= ts >= start
    if end is not None:
        keep &= ts < end + pd.Timedelta(days=1)
    return df[keep]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_training_dataset(actor: str, period_start: str | None = None, period_end: str | None = None,
                            cfg: SimConfig | None = None, out_dir: Path | None = None) -> dict:
    """학습용 정제 데이터 묶음을 만들고 manifest를 돌려준다."""
    start = pd.Timestamp(period_start).normalize() if period_start else None
    end = pd.Timestamp(period_end).normalize() if period_end else None
    if start is not None and end is not None and end < start:
        raise LinkError("종료일이 시작일보다 빠릅니다")
    cfg = cfg or SimConfig()
    ev_df, charger_df, grid_df, is_real = load_scenario(cfg)
    city = build_city_hourly(ev_df, grid_df, cfg)

    excluded = _share_withdrawn(ev_df["ev_id"].unique())
    ev_df = ev_df[~ev_df["ev_id"].isin(excluded)]
    if "ev_id" in charger_df.columns:
        charger_df = charger_df.assign(ev_id=charger_df["ev_id"].where(~charger_df["ev_id"].isin(excluded)))

    origin_ev = "measured" if is_real else "synthetic"
    tables = {
        "ev_telemetry": (_clip_period(ev_df, "timestamp", start, end), origin_ev),
        "charger_sessions": (_clip_period(charger_df, "start_ts", start, end), origin_ev),
        "grid_signal": (_clip_period(grid_df, "timestamp", start, end), "public" if is_real else "synthetic"),
        "city_hourly": (_clip_period(city, "timestamp", start, end), origin_ev),
    }
    export_id = "EXP-" + datetime.now(timezone.utc).strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:8]
    folder = (out_dir or EXPORT_DIR) / export_id
    folder.mkdir(parents=True, exist_ok=True)
    files, counts = [], {}
    for name, (df, origin) in tables.items():
        cols = [c for c in EXPORT_COLUMNS[name] if c in df.columns]
        out = df[cols].copy()
        if "ev_id" in out.columns:   # 가명 ID — 같은 차량은 항상 같은 가명이라 표 사이 조인은 유지된다
            out["ev_id"] = out["ev_id"].map(lambda v: pseudo_id("ev", str(v)) if isinstance(v, str) and v else None)
        out["data_origin"] = origin
        path = folder / f"{name}.csv"
        out.to_csv(path, index=False, encoding="utf-8")
        counts[name] = int(len(out))
        files.append({"name": path.name, "rows": int(len(out)), "columns": list(out.columns), "data_origin": origin,
                      "sha256": _sha256(path)})
    bundle_hash = hashlib.sha256("".join(f["sha256"] for f in files).encode()).hexdigest()
    manifest = {
        "export_id": export_id, "created_at": _now(), "created_by": actor, "recipient": "㈜아크릴",
        "purpose": "AI 예측모델(충전수요·전력수요·가용자원) 학습·검증 — 사업 목적 범위 내 이용 후 파기",
        "period": {"start": start.date().isoformat() if start is not None else None,
                   "end": end.date().isoformat() if end is not None else None},
        "pseudonymized": ["ev_id"], "excluded_vehicles": len(excluded), "is_real_data": bool(is_real),
        "generation": "measured", "files": files, "sha256": bundle_hash,
        "note": None if is_real else "합성(모의) 데이터 묶음 — 연계 형식 점검용이며 실증 데이터가 아님",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    con = store.get_connection()
    try:
        con.execute(
            "INSERT INTO dataset_exports (export_id, created_at, created_by, period_start, period_end, tables, row_counts, "
            "sha256, path) VALUES (?,?,?,?,?,?,?,?,?)",
            (export_id, manifest["created_at"], actor, manifest["period"]["start"], manifest["period"]["end"],
             json.dumps(list(tables)), json.dumps(counts), bundle_hash, str(folder)))
        con.commit()
    finally:
        con.close()
    return manifest


def list_exports(limit: int = 50) -> list[dict]:
    con = store.get_connection()
    try:
        cols = ["export_id", "created_at", "created_by", "period_start", "period_end", "row_counts", "sha256",
                "destroyed_at", "destroyed_by", "destruction_note"]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM dataset_exports ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [{**dict(zip(cols, r)), "row_counts": json.loads(r[5])} for r in rows]


def record_destruction(export_id: str, actor: str, note: str) -> dict:
    """참여기관이 제공 묶음을 파기했음을 기록한다(계획서 6장: 사업 목적 범위 내 이용 후 파기). 기록한 뒤에는 더 내려받을 수 없다.
    note에는 파기 일자·방법·확인서 등 근거를 적는다. 이미 기록된 묶음은 다시 기록하지 않는다."""
    note = " ".join((note or "").split())
    if not _ID_RE.match(export_id or ""):
        raise LinkError("제공 묶음을 찾을 수 없습니다")
    if len(note) < 2 or len(note) > 300:
        raise LinkError("파기 근거(일자·방법·확인서 등)를 2~300자로 적어 주세요")
    con = store.get_connection()
    try:
        row = con.execute("SELECT destroyed_at FROM dataset_exports WHERE export_id=?", (export_id,)).fetchone()
        if not row:
            raise LinkError("제공 묶음을 찾을 수 없습니다")
        if row[0]:
            raise LinkError("이미 파기 확인이 기록된 묶음입니다")
        at = _now()
        con.execute("UPDATE dataset_exports SET destroyed_at=?, destroyed_by=?, destruction_note=? WHERE export_id=?",
                    (at, actor, note, export_id))
        con.commit()
    finally:
        con.close()
    return {"export_id": export_id, "destroyed_at": at, "destroyed_by": actor, "destruction_note": note}


def export_file(export_id: str, name: str) -> Path:
    """제공 묶음 안의 파일 경로. 등록된 묶음의 manifest에 있는 파일 이름만 허용한다(경로 조작 방지)."""
    if not _ID_RE.match(export_id or ""):
        raise LinkError("제공 묶음을 찾을 수 없습니다")
    con = store.get_connection()
    try:
        row = con.execute("SELECT path, destroyed_at FROM dataset_exports WHERE export_id=?", (export_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise LinkError("제공 묶음을 찾을 수 없습니다")
    if row[1]:
        raise LinkError("파기 확인이 기록된 묶음은 더 내려받을 수 없습니다")
    folder = Path(row[0])
    allowed = {"manifest.json"}
    manifest_path = folder / "manifest.json"
    if manifest_path.exists():
        allowed |= {f["name"] for f in json.loads(manifest_path.read_text(encoding="utf-8"))["files"]}
    if name not in allowed or not (folder / name).exists():
        raise LinkError("파일을 찾을 수 없습니다")
    return folder / name


# ------------------------------------------------------------------ 예측 결과 수신 --

def _check_batch(batch: dict) -> tuple[dict, list[tuple]]:
    for key in ("batch_id", "model_id", "model_version"):
        if not _ID_RE.match(str(batch.get(key) or "")):
            raise LinkError(f"{key}는 영문·숫자·._- 1~64자여야 합니다")
    target = batch.get("target")
    if target not in FORECAST_TARGETS:
        raise LinkError(f"target은 {sorted(FORECAST_TARGETS)} 중 하나여야 합니다")
    scope = str(batch.get("scope") or "city")
    if not _ID_RE.match(scope):
        raise LinkError("scope 형식이 올바르지 않습니다")
    try:
        issued = pd.Timestamp(batch["issued_at"])
    except (KeyError, ValueError, TypeError) as exc:
        raise LinkError("issued_at(발행 시각)이 필요합니다") from exc
    points = batch.get("points") or []
    if not points:
        raise LinkError("points가 비어 있습니다")
    if len(points) > MAX_POINTS_PER_BATCH:
        raise LinkError(f"한 배치는 {MAX_POINTS_PER_BATCH}개 이하로 보내 주세요")
    rows, seen = [], set()
    for i, p in enumerate(points):
        try:
            ts = pd.Timestamp(p["timestamp"])
            value = float(p["value"])
        except (KeyError, ValueError, TypeError) as exc:
            raise LinkError(f"points[{i}]: timestamp·value가 필요합니다") from exc
        if ts.tzinfo is not None:   # 내부 시계열은 한국시간 naive
            ts = ts.tz_convert("Asia/Seoul").tz_localize(None)
        lo = float(p["lower"]) if p.get("lower") is not None else None
        hi = float(p["upper"]) if p.get("upper") is not None else None
        if not math.isfinite(value) or value < 0:
            raise LinkError(f"points[{i}]: value는 0 이상 유한한 수여야 합니다")
        if (lo is not None and lo > value) or (hi is not None and hi < value):
            raise LinkError(f"points[{i}]: lower ≤ value ≤ upper 여야 합니다")
        key = ts.isoformat()
        if key in seen:
            raise LinkError(f"points[{i}]: 같은 시각이 두 번 있습니다({key})")
        seen.add(key)
        rows.append((key, value, lo, hi))
    meta = {"batch_id": batch["batch_id"], "model_id": batch["model_id"], "model_version": str(batch["model_version"]),
            "target": target, "scope": scope, "issued_at": issued.isoformat()}
    return meta, rows


def receive_forecast_batch(batch: dict, con: sqlite3.Connection | None = None) -> dict:
    meta, rows = _check_batch(batch)
    own = con is None
    con = con or store.get_connection()
    try:
        received = _now()
        cur = con.executemany(
            "INSERT OR IGNORE INTO external_forecasts (batch_id, provider, model_id, model_version, target, scope, timestamp, "
            "value, lower, upper, issued_at, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(meta["batch_id"], PROVIDER, meta["model_id"], meta["model_version"], meta["target"], meta["scope"],
              ts, v, lo, hi, meta["issued_at"], received) for ts, v, lo, hi in rows])
        con.commit()
        inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else len(rows)
    finally:
        if own:
            con.close()
    return {**meta, "points": len(rows), "inserted": inserted, "duplicate": inserted == 0, "generation": "inferred"}


def latest_forecast(target: str, scope: str = "city", start=None, end=None, con: sqlite3.Connection | None = None) -> pd.DataFrame:
    """시각별로 가장 늦게 발행된 배치의 예측값. 컬럼: timestamp, value, lower, upper, model_version, batch_id, issued_at."""
    if target not in FORECAST_TARGETS:
        raise LinkError(f"알 수 없는 예측 대상입니다: {target}")
    own = con is None
    con = con or store.get_connection()
    try:
        q = ("SELECT timestamp, value, lower, upper, model_version, batch_id, issued_at FROM external_forecasts f "
             "WHERE provider=? AND target=? AND scope=? AND issued_at = (SELECT MAX(issued_at) FROM external_forecasts g "
             "WHERE g.provider=f.provider AND g.target=f.target AND g.scope=f.scope AND g.timestamp=f.timestamp)")
        args: list = [PROVIDER, target, scope]
        if start is not None:
            q += " AND timestamp >= ?"
            args.append(pd.Timestamp(start).isoformat())
        if end is not None:
            q += " AND timestamp <= ?"
            args.append(pd.Timestamp(end).isoformat())
        df = pd.read_sql_query(q + " ORDER BY timestamp", con, params=args)
    finally:
        if own:
            con.close()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df.drop_duplicates("timestamp", keep="last").reset_index(drop=True)


def forecast_status(con: sqlite3.Connection | None = None) -> list[dict]:
    """예측 대상별 수신 현황 — 마지막 배치·모델 버전·수신 시각·예측 구간."""
    own = con is None
    con = con or store.get_connection()
    try:
        out = []
        for target, label in FORECAST_TARGETS.items():
            r = con.execute(
                "SELECT COUNT(DISTINCT batch_id), MAX(received_at), MIN(timestamp), MAX(timestamp) FROM external_forecasts "
                "WHERE provider=? AND target=?", (PROVIDER, target)).fetchone()
            last = con.execute(
                "SELECT batch_id, model_id, model_version, issued_at FROM external_forecasts WHERE provider=? AND target=? "
                "ORDER BY issued_at DESC, received_at DESC LIMIT 1", (PROVIDER, target)).fetchone()
            out.append({"target": target, "label": label, "batches": r[0], "last_received_at": r[1],
                        "covers_from": r[2], "covers_to": r[3],
                        "last_batch": dict(zip(("batch_id", "model_id", "model_version", "issued_at"), last)) if last else None})
        return out
    finally:
        if own:
            con.close()


def evaluate_against_actual(target: str, actual: pd.DataFrame) -> dict:
    """수신한 예측을 같은 시각의 실측(city_hourly 등, timestamp·target 컬럼)과 비교한다 — 계획서 '실측값·예측값 비교 및 오차 분석'."""
    from src.optimizer.effect_metrics import prediction_rate

    fc = latest_forecast(target)
    if fc.empty or target not in actual.columns:
        return {"target": target, "matched_hours": 0, "mae": None, "prediction_rate_pct": None}
    merged = fc.merge(actual[["timestamp", target]], on="timestamp", how="inner")
    if merged.empty:
        return {"target": target, "matched_hours": 0, "mae": None, "prediction_rate_pct": None}
    err = (merged[target] - merged["value"]).abs()
    return {"target": target, "matched_hours": int(len(merged)), "mae": round(float(err.mean()), 3),
            "prediction_rate_pct": prediction_rate(merged[target], merged["value"]),
            "model_versions": sorted(merged["model_version"].unique().tolist())}
