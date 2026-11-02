"""CPO 어댑터 구현체 — 파일(CSV) 기반과 모의(mock). 실제 API 어댑터는 계약·명세 확정 후 추가한다.

CsvFileAdapter: 사업자가 주는 CSV의 컬럼명을 JSON 매핑으로 표준 컬럼에 연결한다.
  새 사업자를 붙일 때 코드 수정 없이 config/cpo_sources.json에 항목만 추가하면 된다.
MockAdapter: 사업자 연계 전 파이프라인·대시보드 검증용. 같은 시드면 같은 결과.
"""
from __future__ import annotations

import json
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
        elif kind == "mock":
            adapters.append(MockAdapter(item["operator_id"], item.get("n_chargers", 5),
                                        item.get("days", 7), item.get("seed", 0)))
        else:
            raise CpoDataError(f"알 수 없는 어댑터 type: {kind}")
    return adapters
