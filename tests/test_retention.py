"""보유기간 파기·접속기록 점검·처리방침 — 체인 유지, 파기 범위, 설정값과 방침 문구의 일치."""
import importlib.util
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.pipeline import store
from src.privacy import access_review, audit, retention
from src.registry import options, service

client = TestClient(app)
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    signup_router.reset_rate_limit()
    yield
    client.post("/auth/logout")


def _log_at(monkeypatch, when: datetime, actor="a", action="x", target=None, detail=None):
    """지정한 시각으로 감사로그 한 줄을 남긴다."""
    monkeypatch.setattr(audit, "_now", lambda: when.isoformat())
    audit.log(actor, action, target=target, detail=detail)


# ------------------------------------------------------- 감사로그 파기·체인 --

def test_purge_removes_old_entries_and_chain_stays_valid(monkeypatch):
    old = NOW - timedelta(days=800)
    for i in range(3):
        _log_at(monkeypatch, old + timedelta(minutes=i), target=f"old{i}")
    for i in range(2):
        _log_at(monkeypatch, NOW - timedelta(days=10) + timedelta(minutes=i), target=f"new{i}")
    assert audit.verify_chain() == {"ok": True, "rows": 5, "broken_at": None}

    cutoff = (NOW - timedelta(days=730)).isoformat()
    assert audit.count_before(cutoff) == 3
    assert audit.purge_before(cutoff) == 3
    assert [e["target"] for e in audit.entries(limit=10)] == ["new1", "new0"]
    assert audit.verify_chain() == {"ok": True, "rows": 2, "broken_at": None}   # 체크포인트부터 검증
    assert audit.purge_before(cutoff) == 0                                        # 다시 해도 변화 없음


def test_chain_continues_after_purging_everything(monkeypatch):
    for i in range(3):
        _log_at(monkeypatch, NOW - timedelta(days=900, minutes=-i))
    assert audit.purge_before((NOW - timedelta(days=10)).isoformat()) == 3
    assert audit.entries(limit=10) == [] and audit.verify_chain()["ok"] is True
    _log_at(monkeypatch, NOW, target="after")        # 비워진 뒤 첫 기록은 체크포인트 해시를 이어받아야 한다
    _log_at(monkeypatch, NOW + timedelta(minutes=1), target="after2")
    assert audit.verify_chain() == {"ok": True, "rows": 2, "broken_at": None}


def test_tampering_is_still_detected_after_purge(monkeypatch):
    for i in range(4):
        _log_at(monkeypatch, NOW - timedelta(days=900 if i < 2 else 5, minutes=-i), target=f"t{i}")
    audit.purge_before((NOW - timedelta(days=730)).isoformat())
    con = sqlite3.connect(audit.consent.PRIVACY_DB)
    con.execute("UPDATE audit_log SET actor='attacker' WHERE target='t2'")
    con.commit()
    con.close()
    assert audit.verify_chain()["ok"] is False


def test_purge_only_removes_a_contiguous_prefix(monkeypatch):
    _log_at(monkeypatch, NOW - timedelta(days=900), target="old")
    _log_at(monkeypatch, NOW - timedelta(days=5), target="recent")
    _log_at(monkeypatch, NOW - timedelta(days=900), target="late-old")   # 시각이 뒤섞인 비정상 기록
    assert audit.purge_before((NOW - timedelta(days=730)).isoformat()) == 1
    assert audit.verify_chain()["ok"] is True       # 중간을 건너뛰어 지우지 않으므로 체인이 깨지지 않는다


# --------------------------------------------------------- 반려 신청 파기 --

def _register(username, plate, status=None, reviewed_days_ago=None):
    auth_store.create_user(username, "driver-pass-1", "owner", allow_unlinked_owner=True)
    reg_id = service.submit(username, plate, "ioniq5", "office", "광주광역시", "북구", options.CONSENT_VERSION)
    if status:
        con = store.get_connection()
        reviewed = (NOW - timedelta(days=reviewed_days_ago)).isoformat() if reviewed_days_ago is not None else None
        con.execute("UPDATE vehicle_registrations SET status=?, reviewed_at=? WHERE registration_id=?",
                    (status, reviewed, reg_id))
        con.commit()
        con.close()
    return reg_id


def _count(status=None):
    con = store.get_connection()
    try:
        q = "SELECT COUNT(*) FROM vehicle_registrations" + (" WHERE status=?" if status else "")
        return con.execute(q, (status,) if status else ()).fetchone()[0]
    finally:
        con.close()


def test_rejected_registrations_purged_only_after_retention():
    _register("old_rej", "11가1111", "rejected", 45)
    _register("new_rej", "22나2222", "rejected", 5)
    _register("approved", "33다3333", "approved", 400)       # 승인된 신청은 오래돼도 지우지 않는다
    _register("pending", "44라4444")                         # 대기 중인 신청도 마찬가지
    assert retention.purge_rejected_registrations(30, NOW, dry_run=True) == 1
    assert _count() == 4                                      # dry-run은 지우지 않는다
    assert retention.purge_rejected_registrations(30, NOW, dry_run=False) == 1
    assert _count() == 3 and _count("rejected") == 1 and _count("approved") == 1 and _count("pending") == 1


def test_run_dry_run_changes_nothing_and_logs_nothing(monkeypatch):
    _log_at(monkeypatch, NOW - timedelta(days=900), target="old")
    _register("old_rej", "11가1111", "rejected", 45)
    before = audit.verify_chain()["rows"]
    result = retention.run(dry_run=True, now=NOW)
    assert result["dry_run"] is True and result["rejected_registrations"] == 1 and result["access_logs"] == 1
    assert _count() == 1 and audit.verify_chain()["rows"] == before


def test_run_apply_purges_and_records_the_purge(monkeypatch):
    _log_at(monkeypatch, NOW - timedelta(days=900), target="old")
    _register("old_rej", "11가1111", "rejected", 45)
    monkeypatch.setattr(audit, "_now", lambda: NOW.isoformat())
    result = retention.run(dry_run=False, now=NOW)
    assert result["rejected_registrations"] == 1 and result["access_logs"] == 1
    assert _count() == 0
    last = audit.entries("retention_purge", limit=1)[0]
    assert last["actor"] == "system" and json.loads(last["detail"])["access_logs"] == 1
    assert audit.verify_chain()["ok"] is True


def test_run_apply_with_nothing_to_purge_leaves_no_log():
    result = retention.run(dry_run=False, now=NOW)
    assert result["rejected_registrations"] == 0 and result["access_logs"] == 0
    assert audit.entries("retention_purge") == []


def test_retention_policy_loading(tmp_path):
    default = retention.load_policy()
    assert default.access_log_days == 730 and default.rejected_registration_days == 30 and default.verified is False
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"access_log_days": 0}), encoding="utf-8")
    with pytest.raises(ValueError):
        retention.load_policy(bad)
    assert retention.load_policy(tmp_path / "missing.json").access_log_days == 730


# ------------------------------------------------------------ 접속기록 점검 --

def test_access_review_flags_bulk_and_off_hours(monkeypatch):
    # 한국시간 23:30 = UTC 14:30 (심야), 같은 날 21건 열람 → 대량 열람
    night = datetime(2026, 10, 21, 14, 30, tzinfo=timezone.utc)
    for i in range(21):
        _log_at(monkeypatch, night + timedelta(seconds=i), actor="opuser", action="pii_view", target=f"REG-{i}")
    _log_at(monkeypatch, NOW - timedelta(hours=3), actor="other", action="registrations_list")
    r = access_review.report(days=30, now=NOW)
    assert r["per_actor"]["opuser"]["pii_view"] == 21 and r["per_actor"]["other"]["registrations_list"] == 1
    assert r["bulk_pii_view"] == [{"actor": "opuser", "date": "2026-10-21", "pii_views": 21}]
    assert len(r["off_hours_pii_view"]) == 21 and r["off_hours_pii_view"][0]["ts_kst"].startswith("2026-10-21T23:30")
    assert set(r["flags"]) == {"bulk_pii_view", "off_hours_pii_view"} and r["chain"]["ok"] is True


def test_access_review_quiet_period_has_no_flags(monkeypatch):
    _log_at(monkeypatch, datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc), actor="opuser", action="pii_view", target="REG-1")  # KST 11시
    r = access_review.report(days=30, now=NOW)
    assert r["flags"] == [] and r["bulk_pii_view"] == [] and r["off_hours_pii_view"] == []


def test_access_review_ignores_events_outside_window(monkeypatch):
    _log_at(monkeypatch, NOW - timedelta(days=90), actor="opuser", action="pii_view", target="REG-1")
    assert access_review.report(days=30, now=NOW)["events"] == 0


def test_access_review_reports_broken_chain(monkeypatch):
    _log_at(monkeypatch, NOW - timedelta(days=1), actor="opuser", action="pii_view", target="REG-1")
    _log_at(monkeypatch, NOW - timedelta(days=1), actor="opuser", action="pii_view", target="REG-2")
    con = sqlite3.connect(audit.consent.PRIVACY_DB)
    con.execute("UPDATE audit_log SET target='REG-X' WHERE target='REG-1'")
    con.commit()
    con.close()
    r = access_review.report(days=30, now=NOW)
    assert "audit_chain_broken" in r["flags"] and r["chain"]["ok"] is False


# --------------------------------------------------------------- 스크립트 --

def _script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_purge_script_is_dry_run_unless_apply(monkeypatch, capsys):
    mod = _script("purge_expired")
    _register("old_rej", "11가1111", "rejected", 45)
    monkeypatch.setattr("sys.argv", ["purge_expired.py"])
    monkeypatch.setattr(retention, "run", lambda dry_run=True, **k: {"dry_run": dry_run, "policy_verified": True})
    assert mod.main() == 0
    assert json.loads(capsys.readouterr().out)["dry_run"] is True
    monkeypatch.setattr("sys.argv", ["purge_expired.py", "--apply"])
    mod.main()
    assert json.loads(capsys.readouterr().out)["dry_run"] is False


def test_access_log_report_script_exit_code_reflects_flags(monkeypatch, capsys):
    mod = _script("access_log_report")
    monkeypatch.setattr("sys.argv", ["access_log_report.py", "--days", "7"])
    monkeypatch.setattr(access_review, "report", lambda days=30: {"flags": ["bulk_pii_view"], "days": days})
    assert mod.main() == 1
    monkeypatch.setattr(access_review, "report", lambda days=30: {"flags": [], "days": days})
    assert mod.main() == 0


# ------------------------------------------------------------- 처리방침 화면 --

def test_policy_page_has_required_sections_and_marks_unconfirmed_items():
    r = client.get("/privacy-policy")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    for section in ("수집하는 개인정보 항목", "이용 목적", "보유 및 이용 기간", "제3자 제공", "처리의 위탁",
                    "정보주체의 권리", "안전성 확보 조치", "개인정보 보호책임자", "침해 대응"):
        assert section in html, section
    assert "초안" in html and "법무 검토 전" in html
    assert html.count("[기재 필요]") >= 4          # 책임자·문의처 등 운영 주체가 정할 값은 비워 둔다
    assert not re.search(r"\d{2,4}-\d{3,4}-\d{4}", html)   # 지어낸 전화번호가 없다
    assert not re.search(r"[\w.]+@[\w.]+\.\w+", html)       # 지어낸 이메일이 없다


def test_policy_retention_text_matches_configured_values():
    """처리방침 문구와 실제 설정이 어긋나지 않게 묶어 둔다."""
    html = client.get("/privacy-policy").text
    policy = retention.load_policy()
    assert f"반려 후 {policy.rejected_registration_days}일" in html
    assert f"{round(policy.access_log_days / 30)}개월" in html


def test_policy_lists_exactly_the_collected_fields():
    html = client.get("/privacy-policy").text
    for field in ("아이디", "비밀번호", "차종", "차량 번호판", "직업군", "거주지"):
        assert field in html
    assert "수집하지 않습니다" in html and "주민등록번호" in html


def test_signup_options_link_the_policy_and_consent_text_mentions_it():
    c = client.get("/signup/options").json()["consent"]
    assert c["policy_url"] == "/privacy-policy" and c["policy_version"].startswith("policy-v")
    assert "개인정보 처리방침" in c["text"]
    assert client.get(c["policy_url"]).status_code == 200
