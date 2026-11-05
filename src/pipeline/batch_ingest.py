"""OBD 일일 배치 수신·정제·반영 파이프라인 (WS1).

OBD 업체는 전날(00:00~24:00 KST) 차량별 원본 CSV를 다음날 새벽 02~03시 사이에 보낸다.
이 모듈은 "받기"와 "반영하기"를 분리해 안전하게 단계적으로 도입하도록 설계했다.

    incoming/  ──(안정화 확인)──▶  raw/<날짜>/  원본 무변경 보관 + SHA-256 기록
                                     │
                          차량별 독립 처리(한 파일 실패가 전체를 막지 않음)
                                     ▼
                     시간당 집계 → 품질 게이트 → staging/<날짜>/<ev_id>.csv
                                     │
              mode=shadow : 여기까지만 (운영 데이터에는 쓰지 않음)
              mode=live   : 검증 통과분만 운영 EV 데이터에 원자적으로 upsert
                                     ▼
                     reports/<일시>.json  (도착 대조표·품질·격리 사유)

운영 원칙
- 멱등: 같은 파일(SHA-256)은 다시 처리하지 않고, (ev_id, timestamp) 키로 upsert한다.
  같은 날짜 데이터의 수정본(다른 해시)이 오면 재처리해 덮어쓴다.
- 안전한 반영: 운영 파일은 임시 파일에 쓴 뒤 교체하고, 반영 직전 백업을 남긴다.
- 격리: 차량 매핑 실패·컬럼 누락(CAN 장애일)·품질 게이트 실패 파일은 quarantine/에 사유와
  함께 격리하고 운영 데이터에는 절대 반영하지 않는다.
- 개인정보: 시간당 집계 결과(staging/운영)에는 GPS·번호판이 없다. 원본(raw/)에는 GPS가
  남아 있으므로 접근을 제한하고 보관기간(`purge_old_raw`)을 둔다.
- 자정을 걸친 세션: 운영 데이터가 시간 단위 행으로 누적되므로 세션 추출(poc_compare)은
  전날 마지막 행과 다음날 첫 행을 자연스럽게 이어서 계산한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from src.paths import DATA_DIR, PROJECT_ROOT
from src.automation import issues
from src.pipeline import schema as schema_module
from src.pipeline import source
from src.pipeline.obd_ingest import (
    ObdColumnsMissingError,
    aggregate_obd_day_to_hourly,
    resolve_obd_columns,
)
from src.pipeline.quality import summarize, validate
from src.privacy import audit, consent

KST = timezone(timedelta(hours=9))
BATCH_DIR = DATA_DIR / "batch"
VEHICLE_REGISTRY_PATH = PROJECT_ROOT / "config" / "obd_vehicles.json"

FILENAME_RE = re.compile(r"^(?P<date>\d{8})-(?P<plate>[^-]+)-(?P<label>.+)\.csv$", re.IGNORECASE)
DEFAULT_MIN_AGE_SECONDS = 120  # 이 시간 이내에 수정된 파일은 아직 전송 중으로 보고 건너뜀
BACKUP_KEEP = 14

MODE_SHADOW = "shadow"
MODE_LIVE = "live"


@dataclass(frozen=True)
class BatchPaths:
    base: Path
    incoming: Path
    raw: Path
    staging: Path
    quarantine: Path
    reports: Path
    backups: Path
    state_db: Path


def make_paths(base: Path | None = None) -> BatchPaths:
    base = Path(base) if base else BATCH_DIR
    paths = BatchPaths(
        base=base,
        incoming=base / "incoming",
        raw=base / "raw",
        staging=base / "staging",
        quarantine=base / "quarantine",
        reports=base / "reports",
        backups=base / "backups",
        state_db=base / "batch_state.db",
    )
    for d in (paths.incoming, paths.raw, paths.staging, paths.quarantine, paths.reports, paths.backups):
        d.mkdir(parents=True, exist_ok=True)
    _init_state(paths)
    return paths


# ----------------------------------------------------------------------------- 상태 저장소

def _connect(paths: BatchPaths) -> sqlite3.Connection:
    return sqlite3.connect(paths.state_db)


def _init_state(paths: BatchPaths) -> None:
    con = _connect(paths)
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS raw_files (
                file_key TEXT PRIMARY KEY,   -- "<sha256>|<파일명>" (다른 차량의 우연한 동일 내용과 구분)
                sha256 TEXT,
                filename TEXT NOT NULL,
                size_bytes INTEGER,
                data_date TEXT,
                plate TEXT,
                ev_id TEXT,
                received_ts TEXT,
                status TEXT,          -- shadow_ok | applied | quarantined | duplicate
                processed_ts TEXT,
                rows INTEGER,
                detail TEXT
            )"""
        )
        con.commit()
    finally:
        con.close()


def _get_status(paths: BatchPaths, sha: str) -> str | None:
    con = _connect(paths)
    try:
        row = con.execute("SELECT status FROM raw_files WHERE file_key=?", (sha,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _upsert_status(paths: BatchPaths, sha: str, **fields) -> None:
    con = _connect(paths)
    try:
        existing = con.execute("SELECT 1 FROM raw_files WHERE file_key=?", (sha,)).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            con.execute(f"UPDATE raw_files SET {sets} WHERE file_key=?", (*fields.values(), sha))
        else:
            cols = ["file_key", "sha256", *fields.keys()]
            con.execute(
                f"INSERT INTO raw_files ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                (sha, sha.split("|", 1)[0], *fields.values()),
            )
        con.commit()
    finally:
        con.close()


def _now_iso() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------------- 파일명·차량 매핑

def parse_filename(name: str) -> dict | None:
    """`20250908-43오3450-토레스 EVX.csv` → {date, plate, label}. 규칙에 안 맞으면 None."""
    m = FILENAME_RE.match(Path(name).name)
    if not m:
        return None
    try:
        d = datetime.strptime(m.group("date"), "%Y%m%d").date()
    except ValueError:
        return None
    return {"date": d, "plate": m.group("plate"), "label": m.group("label")}


def load_registry(path: Path | None = None) -> dict:
    """번호판 → {ev_id, vehicle_model, zone, active}. 파일이 없으면 빈 레지스트리."""
    path = Path(path) if path else VEHICLE_REGISTRY_PATH
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


# ----------------------------------------------------------------------------- 유틸

def sha256_of(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _is_stable(path: Path, min_age_seconds: float, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return (now - path.stat().st_mtime) >= min_age_seconds


def scan_incoming(paths: BatchPaths, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS) -> tuple[list[Path], list[Path]]:
    """(처리 가능 파일, 아직 전송 중으로 보이는 파일). 임시 이름(.part/.tmp)은 무시한다."""
    ready, pending = [], []
    for p in sorted(paths.incoming.glob("*.csv")):
        if p.suffix.lower() != ".csv" or p.name.startswith("."):
            continue
        (ready if _is_stable(p, min_age_seconds) else pending).append(p)
    return ready, pending


def _read_needed_columns(path: Path) -> pd.DataFrame:
    """헤더로 필요한 신호 컬럼만 해석해 그 컬럼만 읽는다 — 하루치 최대 91MB·246컬럼 파일을
    통째로 올리면 메모리를 낭비하므로 집계에 필요한 열만 읽는다."""
    header = list(pd.read_csv(path, nrows=0).columns)
    resolved = resolve_obd_columns(header)
    needed = {c for c in resolved.values() if c}
    for c in ("Date", "SystemTime"):
        if c in header:
            needed.add(c)
    if not needed:
        raise ObdColumnsMissingError("집계에 필요한 컬럼을 하나도 찾지 못했습니다.")
    return pd.read_csv(path, usecols=sorted(needed), low_memory=False)


def _quarantine(paths: BatchPaths, src: Path, reason: str, detail: dict, move: bool = True) -> Path:
    dest_dir = paths.quarantine / reason
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copy2(src, dest)
    dest.with_suffix(dest.suffix + ".json").write_text(
        json.dumps({"reason": reason, **detail}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def _archive_raw(paths: BatchPaths, src: Path, data_date: date | None, move: bool) -> Path:
    folder = paths.raw / (data_date.strftime("%Y%m%d") if data_date else "unknown")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / src.name
    if dest.exists() and sha256_of(dest) != sha256_of(src):
        stamp = datetime.now(KST).strftime("%H%M%S")
        dest = folder / f"{dest.stem}.{stamp}{dest.suffix}"
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copy2(src, dest)
    return dest


# ----------------------------------------------------------------------------- 운영 반영(원자적)

def _backup_operational(paths: BatchPaths) -> Path | None:
    if not source.EV_CSV.exists():
        return None
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    dest = paths.backups / f"ev_telemetry_{stamp}.csv"
    shutil.copy2(source.EV_CSV, dest)
    backups = sorted(paths.backups.glob("ev_telemetry_*.csv"))
    for old in backups[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


def apply_to_operational(paths: BatchPaths, new_rows: pd.DataFrame) -> dict:
    """검증 통과한 시간당 행을 운영 EV 데이터에 (ev_id, timestamp) 키로 upsert한다.
    임시 파일에 쓴 뒤 교체(os.replace)해 쓰는 도중 읽는 쪽이 깨진 파일을 보지 않게 한다."""
    _backup_operational(paths)
    if source.EV_CSV.exists():
        existing = pd.read_csv(source.EV_CSV, parse_dates=["timestamp"])
        combined = pd.concat([existing, new_rows], ignore_index=True)
        combined = combined.drop_duplicates(subset=["ev_id", "timestamp"], keep="last")
    else:
        combined = new_rows
    combined = combined.sort_values(["ev_id", "timestamp"]).reset_index(drop=True)

    quality = validate(combined, "ev_telemetry", schema_module.EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"])
    tmp = source.EV_CSV.with_suffix(".csv.tmp")
    combined.to_csv(tmp, index=False)
    os.replace(tmp, source.EV_CSV)
    source.mark_source("uploaded")
    return {"total_ev_rows": len(combined), "quality_after_apply": quality.passed}


# ----------------------------------------------------------------------------- 파일 단위 처리

def process_file(
    path: Path,
    paths: BatchPaths,
    registry: dict,
    mode: str = MODE_SHADOW,
    live_allowlist: set[str] | None = None,
    move: bool = True,
) -> dict:
    """파일 하나를 처리해 결과 딕셔너리를 반환한다. 예외를 밖으로 던지지 않는다 —
    한 파일의 실패가 같은 날 다른 차량 처리를 막지 않아야 하기 때문이다."""
    path = Path(path)
    result = {"file": path.name, "status": None, "ev_id": None, "date": None}
    try:
        sha = f"{sha256_of(path)}|{path.name}"  # 상태 키: 해시 + 파일명
        size = path.stat().st_size
        meta = parse_filename(path.name)

        prev = _get_status(paths, sha)
        if prev in ("shadow_ok", "applied"):
            result.update(status="duplicate", detail=f"이미 처리된 파일(SHA-256 동일, 이전 상태 {prev})")
            if move:
                path.unlink(missing_ok=True)
            return result

        if meta is None:
            _upsert_status(paths, sha, filename=path.name, size_bytes=size, received_ts=_now_iso(),
                           status="quarantined", processed_ts=_now_iso(), detail="파일명 규칙 불일치")
            _quarantine(paths, path, "bad_filename", {"file": path.name}, move)
            result.update(status="quarantined", reason="bad_filename")
            return result

        result["date"] = meta["date"].isoformat()
        vehicle = registry.get(meta["plate"])
        if not vehicle:
            _upsert_status(paths, sha, filename=path.name, size_bytes=size, data_date=result["date"],
                           plate=meta["plate"], received_ts=_now_iso(), status="quarantined",
                           processed_ts=_now_iso(), detail="미등록 차량")
            _quarantine(paths, path, "unknown_vehicle", {"file": path.name, "plate": meta["plate"]}, move)
            result.update(status="quarantined", reason="unknown_vehicle")
            return result
        ev_id = vehicle["ev_id"]
        result["ev_id"] = ev_id

        # 0) 동의 확인 — CONSENT_ENFORCE=1일 때만 강제한다(동의서 수령 전 시범 운영 단계 보호).
        #    동의가 없으면 처리하지 않고 incoming에 그대로 둔다(상태 기록 없음 → 동의 등록 후 다음 실행에서 처리).
        if os.environ.get("CONSENT_ENFORCE") == "1" and not consent.has_consent(ev_id, "collect"):
            result.update(status="held", reason="no_consent", detail="수집 동의 없음 — 처리 보류")
            return result

        # 1) 원본 보관(무변경) — 이후 처리가 실패해도 원본은 남는다
        archived = _archive_raw(paths, path, meta["date"], move)

        # 2) 정제: 필요한 열만 읽어 시간당 집계
        try:
            df = _read_needed_columns(archived)
            hourly = aggregate_obd_day_to_hourly(
                df, ev_id=ev_id, vehicle_model=vehicle["vehicle_model"], zone=int(vehicle.get("zone", 0)),
            )
        except (ObdColumnsMissingError, ValueError, KeyError) as exc:
            _upsert_status(paths, sha, filename=path.name, size_bytes=size, data_date=result["date"],
                           plate=meta["plate"], ev_id=ev_id, received_ts=_now_iso(), status="quarantined",
                           processed_ts=_now_iso(), detail=str(exc)[:300])
            _quarantine(paths, archived, "unparsable", {"file": path.name, "detail": str(exc)}, move=False)
            result.update(status="quarantined", reason="unparsable", detail=str(exc)[:200])
            issues.record("ingest", f"원천 파일을 해석할 수 없음: {type(exc).__name__}", kind="data",
                          detail={"ev_id": ev_id, "date": result["date"], "error": str(exc)[:200]})
            return result

        # 3) 품질 게이트(결측 1% 미만·중복 0·범위위반 0)
        # 실차 OBD는 차량이 슬립 상태이거나 CAN 신호가 비는 시간대에 SOC가 없다(실측 279일 분석에서
        # 확인). 그 시간대를 결측으로 보고 파일 전체를 격리하면 거의 모든 날이 격리되므로, SOC가 없는
        # 시간 행은 반영 대상에서 제외하고(건수·사유는 리포트에 기록) 남은 행에 게이트를 적용한다.
        hours_total = len(hourly)
        valid = hourly.dropna(subset=["soc_pct"]).reset_index(drop=True)
        file_days = sorted({ts.date().isoformat() for ts in hourly["timestamp"]})
        result.update(hours_total=hours_total, hours_valid=len(valid),
                      hours_dropped_no_soc=hours_total - len(valid), dates_in_file=file_days)
        if valid.empty:
            _upsert_status(paths, sha, filename=path.name, size_bytes=size, data_date=result["date"],
                           plate=meta["plate"], ev_id=ev_id, received_ts=_now_iso(), status="quarantined",
                           processed_ts=_now_iso(), rows=0, detail="유효한 SOC 시간 행 없음")
            _quarantine(paths, archived, "no_valid_hours", {"file": path.name, "hours_total": hours_total}, move=False)
            result.update(status="quarantined", reason="no_valid_hours", rows=0)
            issues.record("ingest", "원천 파일에 유효한 배터리(SOC) 시간 행이 없음", kind="data",
                          detail={"ev_id": ev_id, "date": result["date"], "hours_total": hours_total})
            return result
        hourly = valid
        quality = validate(hourly, "ev_telemetry", schema_module.EV_TELEMETRY_SCHEMA, dedup_keys=["ev_id", "timestamp"])
        result.update(rows=len(hourly),
                      quality={"passed": quality.passed, "missing": quality.missing,
                               "out_of_range": quality.out_of_range, "duplicate_keys": quality.duplicate_keys})
        if not quality.passed:
            _upsert_status(paths, sha, filename=path.name, size_bytes=size, data_date=result["date"],
                           plate=meta["plate"], ev_id=ev_id, received_ts=_now_iso(), status="quarantined",
                           processed_ts=_now_iso(), rows=len(hourly), detail="품질 게이트 실패")
            _quarantine(paths, archived, "quality_gate", {"file": path.name, "quality": result["quality"]}, move=False)
            result.update(status="quarantined", reason="quality_gate")
            issues.record("ingest", "품질 기준 미달(결측·범위 위반·중복)", kind="data",
                          detail={"ev_id": ev_id, "date": result["date"], "quality": result["quality"]})
            return result

        # 4) staging 저장 (shadow든 live든 항상 남긴다)
        stage_dir = paths.staging / meta["date"].strftime("%Y%m%d")
        stage_dir.mkdir(parents=True, exist_ok=True)
        stage_file = stage_dir / f"{ev_id}.csv"
        hourly.to_csv(stage_file, index=False)

        # 5) 반영
        apply_live = mode == MODE_LIVE and (not live_allowlist or ev_id in live_allowlist)
        if apply_live:
            apply_result = apply_to_operational(paths, hourly)
            status = "applied"
            result["apply"] = apply_result
        else:
            status = "shadow_ok"
        _upsert_status(paths, sha, filename=path.name, size_bytes=size, data_date=result["date"],
                       plate=meta["plate"], ev_id=ev_id, received_ts=_now_iso(), status=status,
                       processed_ts=_now_iso(), rows=len(hourly), detail=None)
        result["status"] = status
        return result
    except Exception as exc:  # 예기치 못한 오류도 배치를 멈추지 않는다 — 원인은 리포트에 남긴다
        result.update(status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
        issues.record("ingest", f"원천 파일 처리 중 예기치 못한 오류: {type(exc).__name__}",
                      detail={"date": result.get("date"), "error": str(exc)[:200]})
        return result


# ----------------------------------------------------------------------------- 승격(shadow → 운영)

def promote_staged(paths: BatchPaths, ev_ids: set[str] | None = None) -> dict:
    """shadow로 검증만 해 둔 staging 결과를 운영에 반영한다(그림자 운영 → 1대 반영 → 전체 반영
    단계 전환용). 이미 반영된 항목은 건너뛴다."""
    con = _connect(paths)
    try:
        rows = con.execute(
            "SELECT file_key, ev_id, data_date FROM raw_files WHERE status='shadow_ok' ORDER BY data_date"
        ).fetchall()
    finally:
        con.close()

    applied, skipped = 0, 0
    frames = []
    shas = []
    for sha, ev_id, data_date in rows:
        if ev_ids and ev_id not in ev_ids:
            skipped += 1
            continue
        stage_file = paths.staging / data_date.replace("-", "") / f"{ev_id}.csv"
        if not stage_file.exists():
            skipped += 1
            continue
        frames.append(pd.read_csv(stage_file, parse_dates=["timestamp"]))
        shas.append(sha)
    if frames:
        apply_to_operational(paths, pd.concat(frames, ignore_index=True))
        for sha in shas:
            _upsert_status(paths, sha, status="applied", processed_ts=_now_iso())
        applied = len(shas)
    return {"applied_files": applied, "skipped": skipped}


# ----------------------------------------------------------------------------- 일일 배치

def default_target_date(now: datetime | None = None) -> date:
    """새벽에 도착하는 데이터는 '전날'치다."""
    now = now or datetime.now(KST)
    return (now - timedelta(days=1)).date()


def arrival_table(paths: BatchPaths, registry: dict, target: date) -> dict:
    """예상 차량 대비 도착 현황 — 미도착 차량을 한눈에 본다."""
    expected = {v["ev_id"]: plate for plate, v in registry.items() if v.get("active", True)}
    con = _connect(paths)
    try:
        arrived = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT ev_id, status FROM raw_files WHERE data_date=? AND ev_id IS NOT NULL", (target.isoformat(),)
            ).fetchall()
        }
    finally:
        con.close()
    missing = sorted(ev for ev in expected if ev not in arrived)
    return {
        "target_date": target.isoformat(),
        "expected": len(expected),
        "arrived": sum(1 for ev in expected if ev in arrived),
        "missing_ev_ids": missing,
        "arrived_status": {ev: arrived[ev] for ev in expected if ev in arrived},
    }


def run_daily_batch(
    paths: BatchPaths | None = None,
    mode: str | None = None,
    target_date: date | None = None,
    registry: dict | None = None,
    min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS,
    live_allowlist: set[str] | None = None,
) -> dict:
    """incoming/의 안정화된 파일을 처리하고 리포트를 저장해 반환한다.

    mode 기본값은 환경변수 OBD_BATCH_MODE(없으면 shadow). live에서 일부 차량만 반영하려면
    OBD_BATCH_LIVE_EVS="EV-01,EV-02"처럼 허용 목록을 둔다(1대 반영 단계)."""
    paths = paths or make_paths()
    mode = mode or os.environ.get("OBD_BATCH_MODE", MODE_SHADOW)
    if mode not in (MODE_SHADOW, MODE_LIVE):
        raise ValueError(f"mode는 shadow 또는 live여야 합니다: {mode}")
    if live_allowlist is None:
        env = os.environ.get("OBD_BATCH_LIVE_EVS", "").strip()
        live_allowlist = {x.strip() for x in env.split(",") if x.strip()} or None
    registry = registry if registry is not None else load_registry()
    target = target_date or default_target_date()

    started = _now_iso()
    ready, pending = scan_incoming(paths, min_age_seconds)
    results = [process_file(p, paths, registry, mode, live_allowlist) for p in ready]

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    report = {
        "started": started,
        "finished": _now_iso(),
        "mode": mode,
        "live_allowlist": sorted(live_allowlist) if live_allowlist else None,
        "files_processed": len(results),
        "files_pending_transfer": [p.name for p in pending],
        "counts": counts,
        "arrival": arrival_table(paths, registry, target),
        "results": results,
    }
    audit.log("system:obd_batch", "batch_run", detail={
        "mode": mode, "counts": counts, "files_processed": len(results),
        "missing": len(report["arrival"].get("missing_ev_ids", [])),
    })
    stamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    (paths.reports / f"{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (paths.reports / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def latest_report(paths: BatchPaths | None = None) -> dict | None:
    paths = paths or make_paths()
    f = paths.reports / "latest.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def purge_old_raw(paths: BatchPaths, keep_days: int = 90, today: date | None = None) -> list[str]:
    """보관기간(기본 90일)이 지난 원본(GPS 포함) 폴더를 삭제한다. 집계 결과(staging/운영)는 유지."""
    today = today or datetime.now(KST).date()
    removed = []
    for folder in sorted(paths.raw.iterdir()):
        try:
            d = datetime.strptime(folder.name, "%Y%m%d").date()
        except ValueError:
            continue
        if (today - d).days > keep_days:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
    return removed
