"""OBD 일일 배치(수신→정제→격리/반영) 테스트.

합성 최소 CSV로 핵심 안전장치(멱등·격리·shadow/live·원자적 반영·미도착 대조)를 고정하고,
실제 OBDData 원본이 있으면 실데이터 리플레이로도 검증한다.
"""
import os
import time
from datetime import date

import pandas as pd
import pytest

from src.paths import PROJECT_ROOT
from src.pipeline import batch_ingest as b
from src.pipeline import source

REGISTRY = {
    "11가1111": {"ev_id": "EV-T-01", "vehicle_model": "차종A(세단)", "zone": 0, "active": True},
    "22나2222": {"ev_id": "EV-T-02", "vehicle_model": "차종B(SUV)", "zone": 1, "active": True},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(source, "EV_CSV", tmp_path / "raw" / "ev_telemetry.csv")
    monkeypatch.setattr(source, "SOURCE_MARKER", tmp_path / "raw" / ".source")
    (tmp_path / "raw").mkdir()
    return b.make_paths(tmp_path / "batch")


def _day_csv(path, day="2026-09-01", hours=(9, 10), soc=80.0, with_soc=True):
    rows = []
    for h in hours:
        for sec in (0, 30):
            rows.append({
                "Date": day,
                "SystemTime": float(h * 10000 + sec),
                "VCU_VehicleSpeed_STD(km/h)": 0.0,
                "VCU_BatteryPack_ASOC(%)": soc if with_soc else None,
                "VCU_BatteryPack_Voltage(V)": 380.0,
                "VCU_BatteryPack_Current(A)": 18.0,
                "GPS_longitude": 12655.776,  # 원본에는 위치가 있으나 집계 결과에는 나오면 안 된다
            })
    pd.DataFrame(rows).to_csv(path, index=True)
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def _drop(paths, name, **kw):
    return _day_csv(paths.incoming / name, **kw)


def test_parse_filename():
    meta = b.parse_filename("20250908-43오3450-토레스 EVX.csv")
    assert meta == {"date": date(2025, 9, 8), "plate": "43오3450", "label": "토레스 EVX"}
    assert b.parse_filename("random.csv") is None
    assert b.parse_filename("20251399-43오3450-x.csv") is None  # 존재하지 않는 날짜


def test_scan_incoming_skips_files_still_transferring(env):
    _drop(env, "20260901-11가1111-A.csv")
    fresh = env.incoming / "20260901-22나2222-B.csv"
    _day_csv(fresh)
    os.utime(fresh, None)  # 방금 수정됨 → 아직 전송 중
    ready, pending = b.scan_incoming(env, min_age_seconds=120)
    assert [p.name for p in ready] == ["20260901-11가1111-A.csv"]
    assert [p.name for p in pending] == ["20260901-22나2222-B.csv"]


def test_shadow_mode_validates_but_never_touches_operational_data(env):
    _drop(env, "20260901-11가1111-A.csv")
    report = b.run_daily_batch(env, mode="shadow", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert report["counts"] == {"shadow_ok": 1}
    assert not source.EV_CSV.exists()
    assert (env.staging / "20260901" / "EV-T-01.csv").exists()
    assert list((env.raw / "20260901").glob("*.csv")), "원본은 무변경 보관"
    assert not list(env.incoming.glob("*.csv")), "처리한 파일은 incoming에서 빠진다"
    staged = pd.read_csv(env.staging / "20260901" / "EV-T-01.csv")
    assert not any("GPS" in c for c in staged.columns)  # 위치는 집계 결과에 남지 않는다


def test_live_mode_applies_and_is_idempotent(env):
    _drop(env, "20260901-11가1111-A.csv")
    r1 = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r1["counts"] == {"applied": 1}
    first = pd.read_csv(source.EV_CSV)
    assert set(first["ev_id"]) == {"EV-T-01"}

    # 같은 파일 재전송 → 중복으로 건너뛰고 운영 데이터는 그대로
    _drop(env, "20260901-11가1111-A.csv")
    r2 = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r2["counts"] == {"duplicate": 1}
    assert len(pd.read_csv(source.EV_CSV)) == len(first)


def test_corrected_redelivery_overwrites_same_hours_without_duplicates(env):
    _drop(env, "20260901-11가1111-A.csv", soc=80.0)
    b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    # 같은 날짜의 수정본(내용이 달라 해시가 다름) → 재처리하고 (ev_id, timestamp)로 덮어쓴다
    _drop(env, "20260901-11가1111-A.csv", soc=70.0)
    r = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r["counts"] == {"applied": 1}
    df = pd.read_csv(source.EV_CSV)
    assert not df.duplicated(["ev_id", "timestamp"]).any()
    assert set(df["soc_pct"]) == {70.0}


def test_unknown_vehicle_and_bad_filename_are_quarantined_not_applied(env):
    _drop(env, "20260901-99다9999-X.csv")
    _drop(env, "weird_name.csv")
    report = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert report["counts"] == {"quarantined": 2}
    assert not source.EV_CSV.exists()
    assert (env.quarantine / "unknown_vehicle").exists() and (env.quarantine / "bad_filename").exists()


def test_degraded_log_without_signals_is_quarantined(env):
    p = env.incoming / "20260901-11가1111-A.csv"
    pd.DataFrame({"Date": ["2026-09-01"] * 3, "SystemTime": [10000.0, 10030.0, 10100.0],
                  "ADC_BATT(V)": [12.1, 12.1, 12.0]}).to_csv(p)
    os.utime(p, (time.time() - 3600,) * 2)
    r = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r["counts"] == {"quarantined": 1}
    assert r["results"][0]["reason"] == "unparsable"
    assert not source.EV_CSV.exists()


def test_hours_without_soc_are_dropped_and_reported_not_fatal(env):
    # 슬립 시간대(SOC 없음)가 섞인 실차 패턴: 일부 시간만 유효
    p = env.incoming / "20260901-11가1111-A.csv"
    rows = []
    for h, soc in ((9, 80.0), (10, None), (11, 78.0)):
        for sec in (0, 30):
            rows.append({"Date": "2026-09-01", "SystemTime": float(h * 10000 + sec),
                         "VCU_VehicleSpeed_STD(km/h)": 0.0, "VCU_BatteryPack_ASOC(%)": soc,
                         "VCU_BatteryPack_Voltage(V)": 380.0, "VCU_BatteryPack_Current(A)": 18.0})
    pd.DataFrame(rows).to_csv(p)
    os.utime(p, (time.time() - 3600,) * 2)
    r = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    res = r["results"][0]
    assert res["status"] == "applied"
    assert (res["hours_total"], res["hours_valid"], res["hours_dropped_no_soc"]) == (3, 2, 1)
    assert len(pd.read_csv(source.EV_CSV)) == 2


def test_arrival_table_reports_missing_vehicles(env):
    _drop(env, "20260901-11가1111-A.csv")  # EV-T-02 미도착
    report = b.run_daily_batch(env, mode="shadow", registry=REGISTRY, target_date=date(2026, 9, 1))
    arrival = report["arrival"]
    assert arrival["expected"] == 2 and arrival["arrived"] == 1
    assert arrival["missing_ev_ids"] == ["EV-T-02"]
    # 다음 날 새벽 늦게 도착한 파일은 다음 실행에서 대조표에 반영된다
    _drop(env, "20260901-22나2222-B.csv")
    report2 = b.run_daily_batch(env, mode="shadow", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert report2["arrival"]["missing_ev_ids"] == []


def test_live_allowlist_applies_only_selected_vehicles_then_promote_rest(env):
    _drop(env, "20260901-11가1111-A.csv")
    _drop(env, "20260901-22나2222-B.csv")
    r = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1),
                          live_allowlist={"EV-T-01"})
    assert r["counts"] == {"applied": 1, "shadow_ok": 1}
    assert set(pd.read_csv(source.EV_CSV)["ev_id"]) == {"EV-T-01"}

    promoted = b.promote_staged(env)
    assert promoted["applied_files"] == 1
    assert set(pd.read_csv(source.EV_CSV)["ev_id"]) == {"EV-T-01", "EV-T-02"}
    assert b.promote_staged(env)["applied_files"] == 0  # 이미 반영된 것은 다시 반영하지 않음


def test_operational_write_is_backed_up_and_atomic(env):
    _drop(env, "20260901-11가1111-A.csv")
    b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    _drop(env, "20260902-11가1111-A.csv", day="2026-09-02")
    b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 2))
    assert list(env.backups.glob("ev_telemetry_*.csv")), "두 번째 반영 직전 백업이 있어야 한다"
    assert not source.EV_CSV.with_suffix(".csv.tmp").exists(), "임시 파일이 남으면 안 된다"
    assert len(pd.read_csv(source.EV_CSV)) == 4


def test_one_bad_file_does_not_block_others(env):
    _drop(env, "20260901-11가1111-A.csv")
    (env.incoming / "20260901-22나2222-B.csv").write_text("not,a,valid\n1,2,3\n", encoding="utf-8")
    os.utime(env.incoming / "20260901-22나2222-B.csv", (time.time() - 3600,) * 2)
    r = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r["counts"].get("applied") == 1
    assert sum(v for k, v in r["counts"].items() if k != "applied") == 1


def test_purge_old_raw_removes_only_expired_folders(env):
    for d in ("20250101", "20260820"):
        (env.raw / d).mkdir()
    removed = b.purge_old_raw(env, keep_days=90, today=date(2026, 9, 1))
    assert removed == ["20250101"]
    assert (env.raw / "20260820").exists()


REAL = PROJECT_ROOT / "OBDData" / "20250917-43오3450-토레스 EVX.csv"


@pytest.mark.skipif(not REAL.exists(), reason="OBDData 원본이 없는 체크아웃")
def test_real_file_replay_in_shadow_mode(env):
    registry = b.load_registry()
    assert "43오3450" in registry
    r = b.process_file(REAL, env, registry, "shadow", move=False)
    assert r["status"] == "shadow_ok"
    assert r["hours_valid"] > 0
    assert REAL.exists(), "리플레이는 원본을 옮기지 않는다"


def test_consent_enforced_holds_file_until_consent_registered(env, monkeypatch):
    from src.privacy import audit, consent

    monkeypatch.setenv("CONSENT_ENFORCE", "1")
    _drop(env, "20260901-11가1111-A.csv")
    r1 = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r1["counts"] == {"held": 1}
    assert (env.incoming / "20260901-11가1111-A.csv").exists(), "보류 파일은 incoming에 그대로 남는다"
    assert not source.EV_CSV.exists()

    consent.grant("EV-T-01", "collect", "v1")
    r2 = b.run_daily_batch(env, mode="live", registry=REGISTRY, target_date=date(2026, 9, 1))
    assert r2["counts"] == {"applied": 1}
    assert audit.verify_chain()["ok"]


def test_batch_run_is_audited_without_plates(env):
    from src.privacy import audit

    _drop(env, "20260901-11가1111-A.csv")
    b.run_daily_batch(env, mode="shadow", registry=REGISTRY, target_date=date(2026, 9, 1))
    rows = audit.recent()
    assert rows[0]["action"] == "batch_run" and "11가1111" not in str(rows)
