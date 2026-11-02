"""TOAST Object Storage 수집기 테스트 — 실제 네트워크 없이 가짜 HTTP 세션으로 검증한다."""
import hashlib
from datetime import date

import pytest

from src.pipeline import batch_ingest as b
from src.pipeline import toast_fetch as tf

CFG = tf.ToastConfig("tenant", "user", "pw", "https://store.example/v1/AUTH_t", "bucket", "https://auth.example/tokens")


class FakeResp:
    def __init__(self, status=200, payload=None, content=b""):
        self.status_code, self._payload, self._content = status, payload, content

    def json(self):
        return self._payload

    def iter_content(self, n):
        yield self._content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeSession:
    def __init__(self, objects, files, auth_status=200):
        self.objects, self.files, self.auth_status = objects, files, auth_status
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(("POST", url))
        return FakeResp(self.auth_status, {"access": {"token": {"id": "TOKEN"}}})

    def get(self, url, headers=None, params=None, stream=False, timeout=None):
        self.calls.append(("GET", url))
        assert headers["X-Auth-Token"] == "TOKEN"
        if params is not None:
            return FakeResp(200, self.objects)
        name = url.split("/bucket/")[1]
        from urllib.parse import unquote
        return FakeResp(200, content=self.files[unquote(name)])


def _obj(name, content, etag=None):
    return {"name": name, "bytes": len(content), "hash": etag or hashlib.md5(content).hexdigest(),
            "last_modified": "2026-10-01T19:12:45.840640"}


def test_missing_env_raises_not_configured(monkeypatch):
    for k in ("TOAST_TENANT_ID", "TOAST_USERNAME", "TOAST_PASSWORD", "TOAST_ENDPOINT", "TOAST_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(tf.ToastNotConfigured):
        tf.ToastConfig.from_env()


def test_auth_failure_message_has_no_credentials():
    st = tf.ToastStorage(CFG, FakeSession([], {}, auth_status=401))
    with pytest.raises(tf.ToastError) as e:
        st.list_day(date(2026, 10, 1))
    assert "401" in str(e.value) and "pw" not in str(e.value) and "user" not in str(e.value)


def test_fetch_downloads_verified_files_only_for_selected_plates(tmp_path):
    paths = b.make_paths(tmp_path)
    a, other = b"a,b\n1,2\n", b"x,y\n3,4\n"
    na, no = "2026-10-01/20261001-43오3450-토레스 EVX.csv", "2026-10-01/20261001-05하6942-토레스 EVX.csv"
    st = tf.ToastStorage(CFG, FakeSession([_obj(na, a), _obj(no, other)], {na: a, no: other}))
    r = tf.fetch_day(st, date(2026, 10, 1), paths.incoming, paths.state_db, {"43오3450"})
    assert [d["file"] for d in r["downloaded"]] == ["20261001-43오3450-토레스 EVX.csv"]
    assert r["skipped"][0]["why"] == "not_selected"
    f = paths.incoming / "20261001-43오3450-토레스 EVX.csv"
    assert f.read_bytes() == a and not list(paths.incoming.glob("*.part"))
    # 전송 중 대기에 걸리지 않도록 수정시각이 과거여야 한다
    ready, pending = b.scan_incoming(paths, min_age_seconds=120)
    assert [p.name for p in ready] == [f.name] and not pending
    # 다시 실행하면 받지 않는다
    r2 = tf.fetch_day(st, date(2026, 10, 1), paths.incoming, paths.state_db, {"43오3450"})
    assert r2["downloaded"] == [] and any(s["why"] == "already_handled" for s in r2["skipped"])


def test_checksum_mismatch_is_rejected_and_not_left_behind(tmp_path):
    paths = b.make_paths(tmp_path)
    content = b"a,b\n1,2\n"
    n = "2026-10-01/20261001-43오3450-토레스 EVX.csv"
    bad = _obj(n, content, etag="0" * 32)
    st = tf.ToastStorage(CFG, FakeSession([bad], {n: content}))
    r = tf.fetch_day(st, date(2026, 10, 1), paths.incoming, paths.state_db, None)
    assert r["downloaded"] == [] and len(r["errors"]) == 1
    assert not list(paths.incoming.iterdir())
