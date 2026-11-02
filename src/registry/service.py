"""모바일 가입 차량 등록부 — 신청(pending) → 관리자 승인/반려, 승인 시 계정에 차량 연결.

원칙
- 신청만으로는 차량 데이터에 접근할 수 없다. 관리자가 승인해 계정의 owned_ev_id가 정해져야
  알림·추천이 시작된다(남의 번호판으로 가입해도 그 차량 정보는 보이지 않는다).
- 유효한(대기·승인) 등록은 번호판·계정·차량ID당 하나뿐이다(DB 부분 유니크 인덱스로 보장).
  반려된 신청은 같은 번호판·계정으로 다시 신청할 수 있다.
- 번호판은 평문으로 저장하지 않는다: 암호문(plate_enc) + 중복 확인용 블라인드 인덱스(plate_hash)로만 두고,
  plate 컬럼과 모든 응답에는 마스킹 값만 쓴다. 전체 번호판은 관리자가 사유를 입력해 `reveal_plate`를 호출할
  때만 복호화하며, 그때마다 접속기록(audit)에 남는다.
- 승인 시 ev_id를 지정하면 기존 OBD 차량(EV-TORRES-01 등)과 연결하고, 비우면 EV-U0001 형식으로 새로 부여한다.
- 운행 입력(평소 출발 시각·목표/최소 보장 배터리·누적 주행거리)은 계획서 4.1 '서포터즈 입력' 항목으로, 선택 입력이며
  V2G 의사결정(최적화)의 기본값으로 쓴다.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone

from src.auth import store as auth_store
from src.pipeline import store
from src.privacy import audit, consent, member_history, vault
from src.privacy.pseudonym import mask_plate
from src.registry import options

EV_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,40}$")
AUTO_EV_PREFIX = "EV-U"
STATUSES = ("pending", "approved", "rejected")


class RegistryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_COLS = ("registration_id", "username", "plate", "vehicle_model", "occupation", "region_sido", "region_sigungu",
         "departure_time", "target_soc_pct", "min_soc_pct", "odometer_km",
         "status", "ev_id", "consent_version", "consented_at", "created_at", "reviewed_by", "reviewed_at", "reject_reason")
OPERATING_FIELDS = ("departure_time", "target_soc_pct", "min_soc_pct", "odometer_km")
HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _row(r) -> dict:
    return dict(zip(_COLS, r))


def view(row: dict, catalog: options.VehicleModelCatalog | None = None) -> dict:
    """응답용 표현 — 번호판은 항상 마스킹 값(DB의 plate 컬럼이 이미 마스킹 값). 표시용 라벨을 함께 싣는다."""
    catalog = catalog or options.load_catalog()
    out = dict(row)
    out["vehicle_label"] = catalog.label(row["vehicle_model"]) if catalog.has(row["vehicle_model"]) else row["vehicle_model"]
    out["occupation_label"] = options.OCCUPATIONS.get(row["occupation"], row["occupation"])
    out["region"] = f'{row["region_sido"]} {row["region_sigungu"]}'
    return out


def _check_field(field: str, value: str, catalog: options.VehicleModelCatalog) -> str:
    """항목 하나를 검증하고 정규화한 값을 돌려준다 — 가입·재신청·정정이 같은 규칙을 쓰도록 한 곳에 둔다."""
    if field == "plate":
        value = (value or "").replace(" ", "").strip()
        if not options.PLATE_RE.match(value):
            raise RegistryError("번호판 형식이 올바르지 않습니다(예: 43오3450)")
    elif field == "vehicle_model":
        if not catalog.has(value):
            raise RegistryError("지원하지 않는 차종입니다")
    elif field == "occupation":
        if value not in options.OCCUPATIONS:
            raise RegistryError("직업군을 선택하세요")
    elif field == "region_sido":
        if value not in options.SIDO:
            raise RegistryError("거주 시·도를 선택하세요")
    elif field == "region_sigungu":
        value = " ".join((value or "").split())
        if not options.SIGUNGU_RE.match(value):
            raise RegistryError("시·군·구를 입력하세요(예: 북구, 수원시 영통구) — 읍·면·동과 상세주소는 입력하지 마세요")
    elif field in OPERATING_FIELDS:
        return _check_operating(field, value)
    else:
        raise RegistryError(f"수정할 수 없는 항목입니다: {field}")
    return value


def _check_operating(field: str, value):
    if value is None:
        return None
    if field == "departure_time":
        value = str(value).strip()
        if not HHMM_RE.match(value):
            raise RegistryError("출발 시각은 HH:MM 형식으로 입력하세요(예: 08:00)")
        return value
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise RegistryError(f"숫자로 입력하세요: {field}") from exc
    lo, hi = {"target_soc_pct": (20, 100), "min_soc_pct": (10, 90), "odometer_km": (0, 2_000_000)}[field]
    if not lo <= num <= hi:
        raise RegistryError(f"{field}는 {lo}~{hi} 범위로 입력하세요")
    return num


def operating_inputs(departure_time: str | None = None, target_soc_pct: float | None = None,
                     min_soc_pct: float | None = None, odometer_km: float | None = None) -> dict:
    """서포터즈 운행 입력을 검증해 돌려준다. 최소 보장 배터리는 목표 배터리보다 낮아야 한다."""
    out = {f: _check_operating(f, v) for f, v in (("departure_time", departure_time), ("target_soc_pct", target_soc_pct),
                                                  ("min_soc_pct", min_soc_pct), ("odometer_km", odometer_km))}
    _check_soc_order(out.get("target_soc_pct"), out.get("min_soc_pct"))
    return out


def _check_soc_order(target, minimum) -> None:
    if target is not None and minimum is not None and minimum >= target:
        raise RegistryError("최소 보장 배터리는 목표 배터리보다 낮아야 합니다")


def validate_profile(plate: str, vehicle_model: str, occupation: str, sido: str, sigungu: str,
                     catalog: options.VehicleModelCatalog | None = None) -> dict:
    catalog = catalog or options.load_catalog()
    out = {
        "plate": _check_field("plate", plate, catalog),
        "vehicle_model": _check_field("vehicle_model", vehicle_model, catalog),
        "occupation": _check_field("occupation", occupation, catalog),
        "region_sido": _check_field("region_sido", sido, catalog),
        "region_sigungu": _check_field("region_sigungu", sigungu, catalog),
    }
    if not options.sigungu_allowed(out["region_sido"], out["region_sigungu"]):
        raise RegistryError("선택한 시·도에 없는 시·군·구입니다")
    return out


def submit(username: str, plate: str, vehicle_model: str, occupation: str, sido: str, sigungu: str,
           consent_version: str, con: sqlite3.Connection | None = None, operating: dict | None = None) -> str:
    """차량·페르소나 정보(와 선택 운행 입력)를 신청(pending)으로 저장하고 registration_id를 돌려준다."""
    if consent_version != options.CONSENT_VERSION:
        raise RegistryError("개인정보 수집·이용 동의가 필요합니다")
    p = validate_profile(plate, vehicle_model, occupation, sido, sigungu)
    op = operating_inputs(**(operating or {}))
    registration_id = "REG-" + uuid.uuid4().hex[:12]
    plate_hash = vault.blind_index(p["plate"])
    own = con is None
    con = con or store.get_connection()
    try:
        if con.execute("SELECT 1 FROM vehicle_registrations WHERE username=? AND status IN ('pending','approved')",
                       (username,)).fetchone():
            raise RegistryError("이미 등록 신청이 있거나 승인된 계정입니다")
        if con.execute("SELECT 1 FROM vehicle_registrations WHERE plate_hash=? AND status IN ('pending','approved')",
                       (plate_hash,)).fetchone():
            raise RegistryError("이미 등록되었거나 심사 중인 번호판입니다")
        now = _now()
        try:
            con.execute(
                "INSERT INTO vehicle_registrations (registration_id, username, plate, plate_enc, plate_hash, "
                "vehicle_model, occupation, region_sido, region_sigungu, departure_time, target_soc_pct, min_soc_pct, "
                "odometer_km, status, consent_version, consented_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (registration_id, username, mask_plate(p["plate"]), vault.encrypt(p["plate"]), plate_hash,
                 p["vehicle_model"], p["occupation"], p["region_sido"], p["region_sigungu"],
                 op["departure_time"], op["target_soc_pct"], op["min_soc_pct"], op["odometer_km"],
                 consent_version, now, now),
            )
            con.commit()
        except sqlite3.IntegrityError as exc:  # 동시 신청 경합 — 부분 유니크 인덱스가 최종 방어선
            con.rollback()
            raise RegistryError("이미 등록되었거나 심사 중인 번호판 또는 계정입니다") from exc
    finally:
        if own:
            con.close()
    return registration_id


def reveal_plate(registration_id: str, actor: str, reason: str, ip: str | None = None,
                 con: sqlite3.Connection | None = None) -> str:
    """전체 번호판을 복호화해 돌려준다. 사유(2~100자)가 필수이고, 호출마다 접속기록에 남는다.
    기록에는 번호판 값 자체를 넣지 않는다(등록 ID·사유·접속 IP만)."""
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 100:
        raise RegistryError("열람 사유를 2~100자로 입력하세요")
    own = con is None
    con = con or store.get_connection()
    try:
        r = con.execute("SELECT plate_enc, username FROM vehicle_registrations WHERE registration_id=?",
                        (registration_id,)).fetchone()
    finally:
        if own:
            con.close()
    if not r or not r[0]:
        raise RegistryError("신청을 찾을 수 없습니다")
    audit.log(actor, "pii_view", target=registration_id, detail={"field": "plate", "reason": reason, "ip": ip})
    # 정보주체가 자기 이력에서 "관리자가 번호판을 열람했다"는 사실과 사유를 볼 수 있게 한다
    member_history.record(actor, r[1], "admin_view", ip=ip, note=f"번호판 열람 — {reason}")
    return vault.decrypt(r[0])


def latest_for(username: str, con: sqlite3.Connection | None = None) -> dict | None:
    own = con is None
    con = con or store.get_connection()
    try:
        r = con.execute(
            f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE username=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (username,)).fetchone()
    finally:
        if own:
            con.close()
    return _row(r) if r else None


def list_all(status: str | None = None, limit: int = 200, con: sqlite3.Connection | None = None) -> list[dict]:
    if status is not None and status not in STATUSES:
        raise RegistryError(f"status는 {STATUSES} 중 하나여야 합니다")
    own = con is None
    con = con or store.get_connection()
    try:
        q = f"SELECT {', '.join(_COLS)} FROM vehicle_registrations"
        args: tuple = ()
        if status:
            q += " WHERE status=?"
            args = (status,)
        rows = con.execute(q + " ORDER BY created_at DESC, rowid DESC LIMIT ?", args + (limit,)).fetchall()
    finally:
        if own:
            con.close()
    return [_row(r) for r in rows]


def _next_auto_ev_id(con: sqlite3.Connection) -> str:
    n = 0
    for (ev,) in con.execute("SELECT ev_id FROM vehicle_registrations WHERE ev_id LIKE ?", (AUTO_EV_PREFIX + "%",)):
        m = re.fullmatch(re.escape(AUTO_EV_PREFIX) + r"(\d+)", ev or "")
        if m:
            n = max(n, int(m.group(1)))
    return f"{AUTO_EV_PREFIX}{n + 1:04d}"


def approve(registration_id: str, reviewer: str, ev_id: str | None = None,
            con: sqlite3.Connection | None = None) -> dict:
    """대기 중인 신청을 승인하고 계정에 차량을 연결한다. ev_id를 비우면 EV-U0001 형식으로 새로 부여."""
    ev_id = (ev_id or "").strip() or None
    if ev_id and not EV_ID_RE.match(ev_id):
        raise RegistryError("차량 ID 형식이 올바르지 않습니다(영문·숫자·-·_ 3~40자)")
    own = con is None
    con = con or store.get_connection()
    linked_user = None
    try:
        r = con.execute(f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE registration_id=?",
                        (registration_id,)).fetchone()
        if not r:
            raise RegistryError("신청을 찾을 수 없습니다")
        reg = _row(r)
        if reg["status"] != "pending":
            raise RegistryError(f"이미 처리된 신청입니다({reg['status']})")
        if ev_id is None:
            ev_id = _next_auto_ev_id(con)
        for u in auth_store.list_users():   # 다른 차량 등록자가 이미 쓰는 차량ID는 연결할 수 없다
            if u["role"] == "owner" and u["owned_ev_id"] == ev_id and u["username"] != reg["username"]:
                raise RegistryError("다른 계정에 이미 연결된 차량 ID입니다")
        try:
            cur = con.execute(
                "UPDATE vehicle_registrations SET status='approved', ev_id=?, reviewed_by=?, reviewed_at=? "
                "WHERE registration_id=? AND status='pending'", (ev_id, reviewer, _now(), registration_id))
            if cur.rowcount != 1:
                raise RegistryError("이미 처리된 신청입니다")
            try:
                auth_store.set_owned_vehicle(reg["username"], ev_id)
            except ValueError as exc:   # 신청 후 계정이 삭제된 경우
                raise RegistryError("연결할 계정이 존재하지 않습니다") from exc
            linked_user = reg["username"]
            con.commit()
        except sqlite3.IntegrityError as exc:
            con.rollback()
            raise RegistryError("이미 승인된 차량 ID입니다") from exc
        except Exception:
            con.rollback()
            if linked_user:  # 계정 연결만 먼저 반영된 상태로 남지 않게 되돌린다
                auth_store.set_owned_vehicle(linked_user, None)
            raise
        consent.grant(ev_id, "profile", reg["consent_version"], recorded_by=reg["username"])
        reg.update(status="approved", ev_id=ev_id, reviewed_by=reviewer)
        return reg
    finally:
        if own:
            con.close()


def reject(registration_id: str, reviewer: str, reason: str, con: sqlite3.Connection | None = None) -> dict:
    reason = (reason or "").strip()
    if not 2 <= len(reason) <= 200:
        raise RegistryError("반려 사유는 2~200자로 입력하세요")
    own = con is None
    con = con or store.get_connection()
    try:
        cur = con.execute(
            "UPDATE vehicle_registrations SET status='rejected', reviewed_by=?, reviewed_at=?, reject_reason=? "
            "WHERE registration_id=? AND status='pending'", (reviewer, _now(), reason, registration_id))
        con.commit()
        if cur.rowcount != 1:
            raise RegistryError("대기 중인 신청을 찾을 수 없습니다")
        r = con.execute(f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE registration_id=?",
                        (registration_id,)).fetchone()
        return _row(r)
    finally:
        if own:
            con.close()


def profile_for_ev(ev_id: str, con: sqlite3.Connection | None = None) -> dict | None:
    """승인된 차량의 페르소나(맞춤 알림용). 승인 정보가 없으면 None."""
    own = con is None
    con = con or store.get_connection()
    try:
        r = con.execute(
            f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE ev_id=? AND status='approved'", (ev_id,)).fetchone()
    finally:
        if own:
            con.close()
    if not r:
        return None
    reg = _row(r)
    return {"ev_id": ev_id, "vehicle_model": reg["vehicle_model"], "occupation": reg["occupation"],
            "region_sido": reg["region_sido"], "region_sigungu": reg["region_sigungu"],
            **{f: reg[f] for f in OPERATING_FIELDS}}


EDITABLE_FIELDS = ("vehicle_model", "occupation", "region_sido", "region_sigungu") + OPERATING_FIELDS   # 번호판은 정정 불가(재심사 필요)


def active_for(username: str, con: sqlite3.Connection | None = None) -> dict | None:
    """대기·승인 중인 등록(없으면 None) — 정정은 이 등록에만 할 수 있다. 반려된 신청은 재신청으로 고친다."""
    own = con is None
    con = con or store.get_connection()
    try:
        r = con.execute(
            f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE username=? AND status IN ('pending','approved') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (username,)).fetchone()
    finally:
        if own:
            con.close()
    return _row(r) if r else None


def update_profile(username: str, updates: dict, con: sqlite3.Connection | None = None) -> list[tuple]:
    """정정 요청(차종·직업군·거주지·운행 입력). 바뀐 항목만 반영하고 (field, before, after) 목록을 돌려준다.
    번호판은 소유 확인이 필요해 여기서 바꿀 수 없다(관리자 재심사 경로)."""
    unknown = set(updates) - set(EDITABLE_FIELDS)
    if unknown:
        raise RegistryError(f"수정할 수 없는 항목입니다: {sorted(unknown)[0]}")
    catalog = options.load_catalog()
    own = con is None
    con = con or store.get_connection()
    try:
        r = con.execute(
            f"SELECT {', '.join(_COLS)} FROM vehicle_registrations WHERE username=? AND status IN ('pending','approved') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (username,)).fetchone()
        if not r:
            raise RegistryError("수정할 수 있는 등록이 없습니다(반려된 경우 다시 신청하세요)")
        current = _row(r)
        changes, sets, args = [], [], []
        for field, raw in updates.items():
            value = _check_field(field, raw, catalog)
            if value != current[field]:
                changes.append((field, current[field], value))
                sets.append(f"{field}=?")
                args.append(value)
        if sets:
            merged = {**current, **{f: v for f, _b, v in changes}}   # 정정 후의 시·도와 시·군·구가 서로 맞는지 확인한다
            if not options.sigungu_allowed(merged["region_sido"], merged["region_sigungu"]):
                raise RegistryError("선택한 시·도에 없는 시·군·구입니다")
            _check_soc_order(merged["target_soc_pct"], merged["min_soc_pct"])
            con.execute(f"UPDATE vehicle_registrations SET {', '.join(sets)} WHERE registration_id=?",
                        (*args, current["registration_id"]))
            con.commit()
        return changes
    finally:
        if own:
            con.close()
