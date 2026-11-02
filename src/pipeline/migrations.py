"""curated.db 스키마 버전 관리 — 추가 전용(additive) 마이그레이션.

원칙
- 기존 테이블·컬럼을 지우거나 이름을 바꾸지 않는다(대시보드·분석 코드가 그대로 동작).
- 각 버전은 한 번만 적용되고 `schema_migrations`에 기록된다(재실행해도 안전).
- 컬럼 추가는 `ALTER TABLE ... ADD COLUMN`으로, 테이블이 아직 없으면 건너뛴다
  (이후 `write_table`이 새 컬럼을 포함해 만든다).

v6  vehicle_registrations 번호판 암호화(plate_enc)·블라인드 인덱스(plate_hash), plate 컬럼은 마스킹 값만 저장
v5  vehicle_registrations(모바일 가입 차량·페르소나 정보 — 관리자 승인 전까지 pending)
v4  notifications(차량 등록자 모바일웹 알림함 — 중복 방지 키·읽음 시각)
v3  experiment_groups(실험군/대조군 배정 — 한 번 배정하면 변경하지 않음)
v2  advisories(추천 발행·응답 기록 — 수용률·실험군 분석 근거)
v1  운영 구조 확장
    charger_registry   충전기 마스터(사업자·설치지점·구역·출력·V2G 지원)
    discharge_events   방전(V2G/V2L) 이벤트 기록 — KPI 'B 방전 이벤트' 근거
    charger_sessions   operator_id, source 컬럼 추가(있을 때만)
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
        connector_type TEXT,
        max_kw         REAL,
        v2g_capable    INTEGER NOT NULL DEFAULT 0,
        active         INTEGER NOT NULL DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS discharge_events (
        event_id    TEXT PRIMARY KEY,
        ev_id       TEXT NOT NULL,
        charger_id  TEXT,
        start_ts    TEXT NOT NULL,
        duration_h  REAL NOT NULL CHECK (duration_h >= 0),
        energy_kwh  REAL NOT NULL CHECK (energy_kwh >= 0),
        kind        TEXT NOT NULL CHECK (kind IN ('V2G', 'V2L', 'TEST')),
        source      TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_discharge_ev_ts ON discharge_events (ev_id, start_ts)",
]

_V1_COLUMNS = {"charger_sessions": [("operator_id", "TEXT"), ("source", "TEXT")]}


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
            channel      TEXT NOT NULL CHECK (channel IN ('dashboard', 'app', 'kakao', 'sms', 'api')),
            inputs       TEXT,
            plan         TEXT NOT NULL,
            net_krw      REAL,
            verified     INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'issued'
                         CHECK (status IN ('issued', 'accepted', 'declined', 'expired')),
            responded_at TEXT,
            group_id     TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_advisories_ev ON advisories (ev_id, created_at)")


def _v3(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS experiment_groups (
            experiment_id TEXT NOT NULL,
            ev_id         TEXT NOT NULL,
            group_name    TEXT NOT NULL CHECK (group_name IN ('control', 'treatment')),
            assigned_at   TEXT NOT NULL,
            PRIMARY KEY (experiment_id, ev_id)
        )"""
    )


def _v4(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS notifications (
            notification_id TEXT PRIMARY KEY,
            ev_id           TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            kind            TEXT NOT NULL CHECK (kind IN ('price_window', 'forecast', 'advisory', 'system')),
            title           TEXT NOT NULL,
            body            TEXT NOT NULL,
            payload         TEXT,
            dedupe_key      TEXT,
            read_at         TEXT
        )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS ix_notifications_ev ON notifications (ev_id, created_at)")
    # 같은 차량에 같은 키의 알림이 두 번 쌓이지 않게 한다(주기 작업이 반복돼도 하루 한 건 등)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_dedupe ON notifications (ev_id, dedupe_key) "
        "WHERE dedupe_key IS NOT NULL"
    )


def _v5(con: sqlite3.Connection) -> None:
    con.execute(
        """CREATE TABLE IF NOT EXISTS vehicle_registrations (
            registration_id TEXT PRIMARY KEY,
            username        TEXT NOT NULL,
            plate           TEXT NOT NULL,
            vehicle_model   TEXT NOT NULL,
            occupation      TEXT NOT NULL,
            region_sido     TEXT NOT NULL,
            region_sigungu  TEXT NOT NULL,
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
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_plate ON vehicle_registrations (plate) "
        "WHERE status IN ('pending', 'approved')"
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


def _v6(con: sqlite3.Connection) -> None:
    """번호판을 평문으로 두지 않는다 — 암호문(plate_enc)과 중복 확인용 블라인드 인덱스(plate_hash)로 옮기고,
    기존 plate 컬럼에는 마스킹 값만 남긴다. v5에 평문으로 저장된 행이 있으면 여기서 변환한다."""
    from src.privacy import vault
    from src.privacy.pseudonym import mask_plate

    cols = {r[1] for r in con.execute("PRAGMA table_info(vehicle_registrations)")}
    for name in ("plate_enc", "plate_hash"):
        if name not in cols:
            con.execute(f"ALTER TABLE vehicle_registrations ADD COLUMN {name} TEXT")
    con.execute("DROP INDEX IF EXISTS ux_reg_plate")   # 마스킹 값은 서로 다른 번호판에서 겹칠 수 있다
    for rid, plate in con.execute(
        "SELECT registration_id, plate FROM vehicle_registrations WHERE plate_enc IS NULL"
    ).fetchall():
        con.execute(
            "UPDATE vehicle_registrations SET plate=?, plate_enc=?, plate_hash=? WHERE registration_id=?",
            (mask_plate(plate), vault.encrypt(plate), vault.blind_index(plate), rid),
        )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_reg_plate_hash ON vehicle_registrations (plate_hash) "
        "WHERE plate_hash IS NOT NULL AND status IN ('pending', 'approved')"
    )


MIGRATIONS = [
    (1, "ops_structure", _v1), (2, "advisories", _v2), (3, "experiment_groups", _v3), (4, "notifications", _v4),
    (5, "vehicle_registrations", _v5), (6, "plate_encryption", _v6),
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
