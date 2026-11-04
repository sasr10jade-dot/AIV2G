"""curated.db 스키마 버전 관리 — 추가 전용(additive) 마이그레이션.

원칙
- 기존 테이블·컬럼을 지우거나 이름을 바꾸지 않는다(대시보드·분석 코드가 그대로 동작).
- 각 버전은 한 번만 적용되고 `schema_migrations`에 기록된다(재실행해도 안전).
- 컬럼 추가는 `ALTER TABLE ... ADD COLUMN`으로, 테이블이 아직 없으면 건너뛴다
  (이후 `write_table`이 새 컬럼을 포함해 만든다).

사업수행계획서 v1.0 기준으로 새로 시작한 저장소라 v1~v4가 기준 스키마다.
v5  charger_sessions.site_id(실증 참여 장소 — 계획서 "도시형 참여 장소 3곳 이상" 집계)
v4  AI 예측모델(㈜아크릴) 연계 — 학습 데이터 제공 이력(dataset_exports), 예측 결과 수신(external_forecasts)
v3  vehicle_registrations(서포터즈 가입 — 차량·페르소나·운행 입력, 번호판 암호화·블라인드 인덱스)
v2  advisories(EV별 추천 충전 시간 발행·응답 기록)
v1  charger_registry(충전기 마스터), charger_sessions 계획서 수집 항목 컬럼(있을 때만)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

MIGRATIONS_TABLE = "schema_migrations"

_V1_TABLES = [
    """CREATE TABLE IF NOT EXISTS charger_registry (
        charger_id     TEXT PRIMARY KEY,
        operator_id    TEXT NOT NULL,
        operator_name  TEXT,
        site_id        TEXT,
        zone           INTEGER,
        charger_type   TEXT,
        connector_type TEXT,
        max_kw         REAL,
        v2g_capable    INTEGER NOT NULL DEFAULT 0,
        active         INTEGER NOT NULL DEFAULT 1
    )""",
]

_V1_COLUMNS = {"charger_sessions": [
    ("operator_id", "TEXT"), ("source", "TEXT"), ("end_ts", "TEXT"), ("charger_type", "TEXT"),
    ("connector_type", "TEXT"), ("avg_power_kw", "REAL"), ("stop_reason", "TEXT"), ("error_code", "TEXT"),
]}


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _v1(con: sqlite3.Connection) -> None:
    for ddl in _V1_TABLES:
        con.execute(ddl)
    for table, cols in _V1_COLUMNS.items():
        existing = _columns(con, table)
        if not existing:  # 테이블이 아직 없음
            continue
        for name, typ in cols:
            if name not in existing:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")


def _v2(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS advisories (
            advisory_id  TEXT PRIMARY KEY,
            ev_id        TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            channel      TEXT NOT NULL CHECK (channel IN ('dashboard', 'app', 'api')),
            inputs       TEXT,
            plan         TEXT NOT NULL,
            net_krw      REAL,
            verified     INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'issued'
                         CHECK (status IN ('issued', 'accepted', 'declined', 'expired')),
            responded_at TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_advisories_ev ON advisories (ev_id, created_at)")


def _v3(con: sqlite3.Connection) -> None:
    """번호판은 평문으로 두지 않는다 — 암호문(plate_enc)과 중복 확인용 블라인드 인덱스(plate_hash), plate는 마스킹 값.
    출발 예정 시각·목표/최소 보장 배터리·누적 주행거리는 계획서 4.1 '서포터즈 입력' 항목이다(최적화 입력)."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS vehicle_registrations (
            registration_id TEXT PRIMARY KEY,
            username        TEXT NOT NULL,
            plate           TEXT NOT NULL,
            plate_enc       TEXT,
            plate_hash      TEXT,
            vehicle_model   TEXT NOT NULL,
            occupation      TEXT NOT NULL,
            region_sido     TEXT NOT NULL,
            region_sigungu  TEXT NOT NULL,
            departure_time  TEXT,
            target_soc_pct  REAL CHECK (target_soc_pct IS NULL OR target_soc_pct BETWEEN 20 AND 100),
            min_soc_pct     REAL CHECK (min_soc_pct IS NULL OR min_soc_pct BETWEEN 10 AND 90),
            odometer_km     REAL CHECK (odometer_km IS NULL OR odometer_km >= 0),
            status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
            ev_id           TEXT,
            consent_version TEXT NOT NULL,
            consented_at    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            reviewed_by     TEXT,
            reviewed_at     TEXT,
            reject_reason   TEXT
        )"""
    )
    # 유효한(대기·승인) 등록만 중복을 막는다 — 반려된 번호판·계정은 다시 신청할 수 있다
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_plate_hash ON vehicle_registrations (plate_hash) "
        "WHERE plate_hash IS NOT NULL AND status IN ('pending', 'approved')"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_user ON vehicle_registrations (username) "
        "WHERE status IN ('pending', 'approved')"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_ev ON vehicle_registrations (ev_id) "
        "WHERE ev_id IS NOT NULL AND status = 'approved'"
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_reg_status ON vehicle_registrations (status, created_at)")


def _v4(con: sqlite3.Connection) -> None:
    """㈜아크릴 예측모델 연계(계획서 5.2 기관 간 인터페이스).
    dataset_exports    아우토크립트 → 아크릴 학습용 정제 데이터 제공 이력(가명처리·기간·건수·체크섬)
    external_forecasts 아크릴 → 아우토크립트 예측 추론 결과(일 배치). generation은 항상 추론값이다."""
    con.execute(
        """CREATE TABLE IF NOT EXISTS dataset_exports (
            export_id    TEXT PRIMARY KEY,
            created_at   TEXT NOT NULL,
            created_by   TEXT NOT NULL,
            period_start TEXT,
            period_end   TEXT,
            tables       TEXT NOT NULL,
            row_counts   TEXT NOT NULL,
            sha256       TEXT NOT NULL,
            path         TEXT NOT NULL
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS external_forecasts (
            batch_id     TEXT NOT NULL,
            provider     TEXT NOT NULL,
            model_id     TEXT NOT NULL,
            model_version TEXT NOT NULL,
            target       TEXT NOT NULL,
            scope        TEXT NOT NULL DEFAULT 'city',
            timestamp    TEXT NOT NULL,
            value        REAL NOT NULL,
            lower        REAL,
            upper        REAL,
            issued_at    TEXT NOT NULL,
            received_at  TEXT NOT NULL,
            PRIMARY KEY (provider, target, scope, timestamp, batch_id)
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_extfc_target ON external_forecasts (target, scope, timestamp)")


def _v5(con: sqlite3.Connection) -> None:
    existing = _columns(con, "charger_sessions")
    if existing and "site_id" not in existing:
        con.execute("ALTER TABLE charger_sessions ADD COLUMN site_id TEXT")


MIGRATIONS = [
    (1, "ops_structure", _v1), (2, "advisories", _v2), (3, "vehicle_registrations", _v3),
    (4, "acryl_forecast_link", _v4), (5, "charger_site", _v5),
]
LATEST_VERSION = MIGRATIONS[-1][0]


def current_version(con: sqlite3.Connection) -> int:
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = con.execute(f"SELECT MAX(version) FROM {MIGRATIONS_TABLE}").fetchone()
    return int(row[0] or 0)


def migrate(con: sqlite3.Connection) -> list[int]:
    """미적용 버전을 순서대로 적용하고, 이번에 적용한 버전 목록을 반환한다.
    각 버전은 하나의 트랜잭션 — 실패하면 그 버전만 롤백하고 예외를 올린다."""
    applied: list[int] = []
    have = current_version(con)
    for version, name, fn in MIGRATIONS:
        if version <= have:
            continue
        if con.in_transaction:
            con.commit()
        try:
            con.execute("BEGIN")
            fn(con)
            con.execute(
                f"INSERT INTO {MIGRATIONS_TABLE} (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, datetime.now(timezone.utc).isoformat()),
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied
