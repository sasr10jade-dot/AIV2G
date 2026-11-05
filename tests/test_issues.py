"""모델링 예외 기록(계획서 4.1 다) — 같은 원인 묶기·조치 완료·재발·관리 API."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.automation import issues

client = TestClient(app)


def test_same_cause_is_grouped_and_counted():
    issues.record("forecast", "수요 예측을 불러오지 못함: FileNotFoundError", detail={"error": "a"})
    issues.record("forecast", "수요 예측을 불러오지 못함: FileNotFoundError", detail={"error": "b"})
    issues.record("safety", "안전 검증 실패(min_soc) — 방전 없는 계획으로 복귀", kind="exception")
    items = issues.list_issues()
    assert len(items) == 2
    fc = next(x for x in items if x["stage"] == "forecast")
    assert fc["count"] == 2 and fc["detail"] == {"error": "b"} and fc["stage_label"] == "수요 예측"
    sm = issues.summary()
    assert sm["open"] == 2 and sm["occurrences"] == 3 and sm["by_stage"]["safety"]["open"] == 1


def test_record_never_raises_on_bad_input():
    issues.record("nope", "x")                     # 알 수 없는 단계는 기록하지 않고 조용히 넘어간다
    issues.record("forecast", "x", kind="nope")
    assert issues.list_issues("all") == []


def test_resolve_then_recurrence_opens_new_row():
    issues.record("ingest", "품질 기준 미달(결측·범위 위반·중복)", kind="data")
    iid = issues.list_issues()[0]["issue_id"]
    with pytest.raises(issues.IssueError):
        issues.resolve(iid, "op", "x")             # 조치 내용이 너무 짧다
    done = issues.resolve(iid, "op", "SOC 범위 검사 기준 보완")
    assert done["status"] == "resolved" and done["resolved_by"] == "op"
    with pytest.raises(issues.IssueError, match="이미"):
        issues.resolve(iid, "op", "다시")
    issues.record("ingest", "품질 기준 미달(결측·범위 위반·중복)", kind="data")
    assert [x["issue_id"] for x in issues.list_issues()] != [iid]          # 재발은 새 줄
    assert len(issues.list_issues("all")) == 2 and issues.summary()["resolved"] == 1


def test_admin_api_lists_and_resolves(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    auth_store.create_user("test-admin", "test-password-1", "admin")
    client.post("/auth/login", json={"username": "test-admin", "password": "test-password-1"})
    try:
        issues.record("retrain", "재학습 후보가 기존 모델보다 충분히 낫지 않아 승격 거부(기존 모델 유지)", kind="exception")
        r = client.get("/admin/modeling-issues")
        assert r.status_code == 200 and r.json()["summary"]["open"] == 1
        iid = r.json()["items"][0]["issue_id"]
        assert client.get("/admin/modeling-issues?status=bad").status_code == 422
        r = client.post(f"/admin/modeling-issues/{iid}/resolve", json={"note": "개선 기준 2% → 1%로 조정 검토"})
        assert r.status_code == 200 and r.json()["status"] == "resolved"
        assert client.post(f"/admin/modeling-issues/{iid}/resolve", json={"note": "다시 닫기"}).status_code == 409
        assert client.get("/admin/modeling-issues?status=resolved").json()["items"][0]["issue_id"] == iid
    finally:
        client.post("/auth/logout")
    assert client.get("/admin/modeling-issues").status_code in (401, 403)
