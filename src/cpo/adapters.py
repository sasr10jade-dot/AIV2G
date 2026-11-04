"""CPO 어댑터 구현체 — REST API·파일(CSV)·모의(mock). REST는 데이터 제공 용역의 API 명세를 설정으로 받는다.

CsvFileAdapter: 사업자가 주는 CSV의 컬럼명을 JSON 매핑으로 표준 컬럼에 연결한다.
  새 사업자를 붙일 때 코드 수정 없이 config/cpo_sources.json에 항목만 추가하면 된다.
MockAdapter: 사업자 연계 전 파이프라인·대시보드 검증용. 같은 시드면 같은 결과.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.paths import PROJECT_ROOT

from .base import SESSION_COLUMNS, CpoAdapter, CpoDataError, normalize_sessions

CPO_CONFIG = PROJECT_ROOT / "config" / "cpo_sources.json"


class CsvFileAdapter(CpoAdapter):
    source_kind = "cpo_file"

    def __init__(self, operator_id: str, path: str | Path, column_map: dict[str, str],
                 time_format: str | None = None, energy_unit: str = "kwh"):
        """column_map: {표준컬럼: 사업자 컬럼명}. energy_unit이 "wh"면 kWh로 환산한다."""
        self.operator_id = operator_id
        self.path = Path(path)
        self.column_map = column_map
        self.time_format = time_format
        self.energy_unit = energy_unit.lower()

    def fetch_sessions(self, since: datetime | None = None) -> pd.DataFrame:
        if not self.path.exists():
            return pd.DataFrame(columns=SESSION_COLUMNS)
        raw = pd.read_csv(self.path, dtype=str, encoding="utf-8-sig")
        missing = [src for std, src in self.column_map.items() if std != "ev_id" and src not in raw.columns]
        if missing:
            raise CpoDataError(f"{self.operator_id}: CSV에 매핑된 컬럼이 없습니다: {missing}")
        df = pd.DataFrame({std: raw[src] for std, src in self.column_map.items() if src in raw.columns})
        if self.time_format and "start_ts" in df:
            df["start_ts"] = pd.to_datetime(df["start_ts"], format=self.time_format, errors="coerce")
        if "zone" not in df:
            df["zone"] = 0
        df = normalize_sessions(df)
        if self.energy_unit == "wh":
            df["energy_kwh"] = df["energy_kwh"] / 1000.0
        if since is not None:
            df = df[df["start_ts"] >= pd.Timestamp(since)]
        return df.reset_index(drop=True)


class MockAdapter(CpoAdapter):
    source_kind = "cpo_mock"

    def __init__(self, operator_id: str, n_chargers: int = 5, days: int = 7, seed: int = 0,
                 start: str = "2026-09-01"):
        self.operator_id = operator_id
        self.n_chargers, self.days, self.seed, self.start = n_chargers, days, seed, start

    def fetch_sessions(self, since: datetime | None = None) -> pd.DataFrame:
        rng = np.random.default_rng(self.seed)
        rows = []
        for c in range(self.n_chargers):
            for d in range(self.days):
                for k in range(rng.poisson(0.5)):
                    ts = pd.Timestamp(self.start) + pd.Timedelta(days=d, hours=float(rng.uniform(0, 24)))
                    dur = round(float(max(rng.normal(2.5, 0.8), 0.3)), 2)
                    rows.append((f"{self.operator_id}-S{c}-{d}-{k}", f"{self.operator_id}-CHG-{c:03d}", 0, ts,
                                 dur, round(dur * 7.0 * float(rng.uniform(0.7, 1.0)), 2), None))
        df = pd.DataFrame(rows, columns=SESSION_COLUMNS)
        if since is not None:
            df = df[df["start_ts"] >= pd.Timestamp(since)]
        return df.reset_index(drop=True)


class RestApiAdapter(CpoAdapter):
    """충전사업자 REST API 어댑터 — 데이터 제공 용역(계획서 13장)의 "아우토크립트 제공용 REST API"를 일 단위로 가져온다.

    사업자마다 주소·인증·파라미터·응답 필드 이름이 달라 코드에 박지 않고 설정(config/cpo_sources.json, type=rest)으로 둔다.
    인증 값은 설정 파일이 아니라 환경변수(`auth_env`)에서 읽는다. 응답은 JSON이며 `items_path`(점 구분)에 세션 목록이 있다.
    페이지는 `page_param`을 1부터 올리며 빈 페이지가 오거나 `max_pages`에 닿으면 멈춘다.
    """
    source_kind = "cpo_api"

    def __init__(self, operator_id: str, url: str, column_map: dict[str, str], items_path: str = "items",
                 auth_header: str = "Authorization", auth_env: str | None = None, auth_prefix: str = "Bearer ",
                 since_param: str | None = "since", since_format: str = "%Y-%m-%d", page_param: str | None = "page",
                 page_size_param: str | None = "size", page_size: int = 500, max_pages: int = 50,
                 energy_unit: str = "kwh", timeout: float = 20.0, session=None):
        self.operator_id = operator_id
        self.url, self.column_map, self.items_path = url, column_map, items_path
        self.auth_header, self.auth_env, self.auth_prefix = auth_header, auth_env, auth_prefix
        self.since_param, self.since_format = since_param, since_format
        self.page_param, self.page_size_param, self.page_size, self.max_pages = page_param, page_size_param, page_size, max_pages
        self.energy_unit, self.timeout = energy_unit.lower(), timeout
        self._session = session

    @classmethod
    def from_config(cls, item: dict) -> "RestApiAdapter":
        keys = ("items_path", "auth_header", "auth_env", "auth_prefix", "since_param", "since_format", "page_param",
                "page_size_param", "page_size", "max_pages", "energy_unit", "timeout")
        return cls(item["operator_id"], item["url"], item["column_map"], **{k: item[k] for k in keys if k in item})

    def _headers(self) -> dict:
        if not self.auth_env:
            return {}
        token = os.environ.get(self.auth_env, "")
        if not token:
            raise CpoDataError(f"{self.operator_id}: 인증 환경변수 {self.auth_env}가 비어 있습니다")
        return {self.auth_header: f"{self.auth_prefix}{token}"}

    def _items(self, payload) -> list[dict]:
        node = payload
        for key in [k for k in self.items_path.split(".") if k]:
            if not isinstance(node, dict) or key not in node:
                raise CpoDataError(f"{self.operator_id}: 응답에 '{self.items_path}' 경로가 없습니다")
            node = node[key]
        if isinstance(node, dict):   # 항목이 하나면 목록이 아니라 객체로 오는 API가 있다
            node = [node]
        if not isinstance(node, list):
            raise CpoDataError(f"{self.operator_id}: '{self.items_path}'가 목록이 아닙니다")
        return node

    def fetch_sessions(self, since: datetime | None = None) -> pd.DataFrame:
        import requests

        http = self._session or requests.Session()
        params: dict = {}
        if since is not None and self.since_param:
            params[self.since_param] = pd.Timestamp(since).strftime(self.since_format)
        if self.page_size_param:
            params[self.page_size_param] = self.page_size
        rows: list[dict] = []
        for page in range(1, self.max_pages + 1):
            if self.page_param:
                params[self.page_param] = page
            resp = http.get(self.url, params=dict(params), headers=self._headers(), timeout=self.timeout)
            resp.raise_for_status()
            items = self._items(resp.json())
            rows.extend(items)
            if not self.page_param or not items or (self.page_size_param and len(items) < self.page_size):
                break
        else:
            raise CpoDataError(f"{self.operator_id}: 페이지가 {self.max_pages}쪽을 넘습니다(설정 확인 필요)")
        if not rows:
            return pd.DataFrame(columns=SESSION_COLUMNS)
        raw = pd.DataFrame(rows)
        missing = [src for std, src in self.column_map.items() if std in SESSION_COLUMNS and std != "ev_id" and src not in raw.columns]
        if missing:
            raise CpoDataError(f"{self.operator_id}: 응답에 매핑된 필드가 없습니다: {missing}")
        df = pd.DataFrame({std: raw[src] for std, src in self.column_map.items() if src in raw.columns})
        if "zone" not in df:
            df["zone"] = 0
        if "duration_h" not in df and "end_ts" in df:   # 시작·종료 시각만 주는 사업자 — 충전 시간은 계산한다
            df["duration_h"] = (pd.to_datetime(df["end_ts"]) - pd.to_datetime(df["start_ts"])).dt.total_seconds() / 3600
        df = normalize_sessions(df)
        if self.energy_unit == "wh":
            df["energy_kwh"] = df["energy_kwh"] / 1000.0
        if since is not None:
            df = df[df["start_ts"] >= pd.Timestamp(since)]
        return df.reset_index(drop=True)


def load_adapters(config_path: Path | None = None) -> list[CpoAdapter]:
    """config/cpo_sources.json의 활성 항목으로 어댑터 목록을 만든다.
    항목 예: {"operator_id": "CPO-A", "type": "csv", "path": "data/cpo/cpo_a.csv",
              "column_map": {"session_id": "txn_id", ...}, "enabled": true}"""
    path = config_path or CPO_CONFIG
    if not Path(path).exists():
        return []
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    adapters: list[CpoAdapter] = []
    for item in cfg.get("sources", []):
        if not item.get("enabled", False):
            continue
        kind = item.get("type")
        if kind == "csv":
            p = Path(item["path"])
            adapters.append(CsvFileAdapter(
                item["operator_id"], p if p.is_absolute() else PROJECT_ROOT / p,
                item["column_map"], item.get("time_format"), item.get("energy_unit", "kwh"),
            ))
        elif kind == "rest":
            adapters.append(RestApiAdapter.from_config(item))
        elif kind == "mock":
            adapters.append(MockAdapter(item["operator_id"], item.get("n_chargers", 5),
                                        item.get("days", 7), item.get("seed", 0)))
        else:
            raise CpoDataError(f"알 수 없는 어댑터 type: {kind}")
    return adapters
