"""NHN Cloud(TOAST) Object Storage에서 OBD 일일 CSV를 가져와 배치의 incoming/에 내려놓는다.

버킷 구조(확인됨): `<버킷>/YYYY-MM-DD/YYYYMMDD-<번호판>-<차명>.csv` — 파일명 규칙이 배치의
`parse_filename`과 같아 내려받은 파일을 그대로 `run_daily_batch`가 처리한다.

안전장치
- 인증정보는 환경변수(.env)에서만 읽는다: TOAST_TENANT_ID, TOAST_USERNAME, TOAST_PASSWORD,
  TOAST_ENDPOINT, TOAST_BUCKET (선택: TOAST_AUTH_URL). 코드·로그·리포트에 남기지 않는다.
- 읽기 전용: 목록 조회(GET)와 다운로드(GET)만 한다. 버킷의 객체를 쓰거나 지우지 않는다.
- 다운로드는 `.part`로 받아 크기·ETag(MD5) 검증 후 이름을 바꾼다 — 전송이 끊긴 파일이
  incoming에 완성본처럼 남지 않는다.
- 이미 처리했거나(raw_files 상태에 같은 파일명·크기) incoming에 있는 파일은 다시 받지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

DEFAULT_AUTH_URL = "https://api-identity-infrastructure.nhncloudservice.com/v2.0/tokens"
CHUNK = 1024 * 1024


class ToastNotConfigured(Exception):
    """필수 환경변수가 없을 때."""


class ToastError(Exception):
    """인증·조회·다운로드 실패(상태코드만 담고 인증정보는 담지 않는다)."""


@dataclass
class ToastConfig:
    tenant_id: str
    username: str
    password: str
    endpoint: str
    bucket: str
    auth_url: str = DEFAULT_AUTH_URL

    @classmethod
    def from_env(cls) -> "ToastConfig":
        need = ["TOAST_TENANT_ID", "TOAST_USERNAME", "TOAST_PASSWORD", "TOAST_ENDPOINT", "TOAST_BUCKET"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            raise ToastNotConfigured(f"환경변수가 없습니다: {', '.join(missing)}")
        return cls(
            tenant_id=os.environ["TOAST_TENANT_ID"], username=os.environ["TOAST_USERNAME"],
            password=os.environ["TOAST_PASSWORD"], endpoint=os.environ["TOAST_ENDPOINT"].rstrip("/"),
            bucket=os.environ["TOAST_BUCKET"], auth_url=os.environ.get("TOAST_AUTH_URL", DEFAULT_AUTH_URL),
        )


class ToastStorage:
    def __init__(self, cfg: ToastConfig, session: requests.Session | None = None, timeout: float = 30.0):
        self.cfg = cfg
        self.http = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_until = 0.0

    # ------------------------------------------------------------ 인증
    def _auth(self) -> str:
        if self._token and time.time() < self._token_until:
            return self._token
        r = self.http.post(
            self.cfg.auth_url,
            json={"auth": {"tenantId": self.cfg.tenant_id,
                           "passwordCredentials": {"username": self.cfg.username, "password": self.cfg.password}}},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            raise ToastError(f"인증 실패 (HTTP {r.status_code})")
        self._token = r.json()["access"]["token"]["id"]
        self._token_until = time.time() + 30 * 60  # 토큰 수명보다 짧게 잡아 만료 전에 갱신
        return self._token

    def _headers(self) -> dict:
        return {"X-Auth-Token": self._auth()}

    # ------------------------------------------------------------ 조회
    def list_day(self, day: date) -> list[dict]:
        """해당 날짜 폴더의 객체 목록 [{name, bytes, hash, last_modified}]."""
        prefix = f"{day.isoformat()}/"
        out: list[dict] = []
        marker = None
        while True:
            params = {"format": "json", "limit": 1000, "prefix": prefix}
            if marker:
                params["marker"] = marker
            r = self.http.get(f"{self.cfg.endpoint}/{self.cfg.bucket}", headers=self._headers(),
                              params=params, timeout=self.timeout)
            if r.status_code != 200:
                raise ToastError(f"목록 조회 실패 (HTTP {r.status_code})")
            page = r.json()
            out += [o for o in page if o.get("name", "").lower().endswith(".csv")]
            if len(page) < 1000:
                return out
            marker = page[-1]["name"]

    # ------------------------------------------------------------ 다운로드
    def download(self, obj: dict, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        md5 = hashlib.md5()
        size = 0
        url = f"{self.cfg.endpoint}/{self.cfg.bucket}/{requests.utils.quote(obj['name'])}"
        with self.http.get(url, headers=self._headers(), stream=True, timeout=self.timeout) as r:
            if r.status_code != 200:
                raise ToastError(f"다운로드 실패 (HTTP {r.status_code}): {Path(obj['name']).name}")
            with open(part, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    f.write(chunk)
                    md5.update(chunk)
                    size += len(chunk)
        etag = (obj.get("hash") or "").strip('"')
        if size != obj.get("bytes") or (etag and len(etag) == 32 and md5.hexdigest() != etag):
            part.unlink(missing_ok=True)
            raise ToastError(f"검증 실패(크기/체크섬 불일치): {Path(obj['name']).name}")
        part.replace(dest)  # 검증 후에만 완성본 이름으로
        return dest


def _already_handled(state_db: Path, filename: str, size: int) -> bool:
    con = sqlite3.connect(state_db)
    try:
        row = con.execute(
            "SELECT 1 FROM raw_files WHERE filename=? AND size_bytes=? AND status IN ('shadow_ok','applied','quarantined') LIMIT 1",
            (filename, size),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _mark_complete(dest: Path, obj: dict) -> None:
    """검증을 마친 파일이므로 배치의 '전송 중' 대기(최소 경과 시간)에 걸리지 않도록 수정시각을
    버킷 객체의 업로드 시각으로 맞춘다."""
    try:
        ts = datetime.fromisoformat(obj["last_modified"]).replace(tzinfo=timezone.utc).timestamp()
    except (KeyError, ValueError):
        ts = time.time() - 3600
    os.utime(dest, (ts, ts))


def fetch_day(storage: ToastStorage, day: date, incoming: Path, state_db: Path, only_plates: set[str] | None = None) -> dict:
    """한 날짜 폴더를 incoming/으로 가져온다. 객체 단위로 오류를 격리한다."""
    result = {"date": day.isoformat(), "listed": 0, "downloaded": [], "skipped": [], "errors": []}
    objs = storage.list_day(day)
    result["listed"] = len(objs)
    for obj in objs:
        name = Path(obj["name"]).name
        parts = name.split("-")
        if only_plates is not None and (len(parts) < 3 or parts[1] not in only_plates):
            result["skipped"].append({"file": name, "why": "not_selected"})
            continue
        dest = incoming / name
        if dest.exists() or _already_handled(state_db, name, int(obj["bytes"])):
            result["skipped"].append({"file": name, "why": "already_handled"})
            continue
        try:
            storage.download(obj, dest)
            _mark_complete(dest, obj)
            result["downloaded"].append({"file": name, "bytes": obj["bytes"]})
        except (ToastError, requests.RequestException, OSError) as exc:
            result["errors"].append({"file": name, "error": str(exc)[:200]})
    return result
