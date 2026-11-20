"""범용 데이터 표준화 — 매핑 레지스트리(config/standard_mapping.json)와 표준 형식 출력(평가의견 1).

공통 스키마(src/pipeline/schema.py) 필드마다 COVESA VSS·OCPP 1.6J·OCPI 2.2.1 대응 명칭을 병기하고, 2차년도
외부 연계 때 `to_standard()`로 표준 명칭 열을 가진 표로 출력한다. 식별자(ev_id)는 가명 ID로 바꿔 내보낸다.
대응 명칭이 없는 필드는 출력에서 뺀다(지어내지 않는다).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src.paths import PROJECT_ROOT

MAPPING_FILE = PROJECT_ROOT / "config" / "standard_mapping.json"
STANDARDS = ("vss", "ocpp", "ocpi")


def load_mapping(path: Path | None = None) -> dict:
    return json.loads(Path(path or MAPPING_FILE).read_text(encoding="utf-8"))


def coverage(mapping: dict | None = None) -> dict:
    """표·표준별로 대응 명칭이 있는 필드 수."""
    m = mapping or load_mapping()
    return {table: {std: sum(1 for f in fields if f.get(std) and not str(f[std]).startswith("(")) for std in STANDARDS} | {"fields": len(fields)}
            for table, fields in m["tables"].items()}


def to_standard(df: pd.DataFrame, table: str, standard: str, mapping: dict | None = None) -> pd.DataFrame:
    """공통 스키마 표 → 표준 명칭 열. ev_id는 가명(PE-…), 대응 없는 필드는 제외."""
    from src.privacy.pseudonym import pseudo_id
    m = mapping or load_mapping()
    if table not in m["tables"]:
        raise KeyError(table)
    if standard not in STANDARDS:
        raise ValueError(standard)
    cols = {}
    for f in m["tables"][table]:
        name = f.get(standard)
        if not name or str(name).startswith("(") or f["field"] not in df.columns:
            continue
        cols[f["field"]] = name
    out = df[list(cols)].copy()
    if "ev_id" in out.columns:
        out["ev_id"] = out["ev_id"].map(lambda v: pseudo_id("ev", str(v)) if pd.notna(v) and str(v) else None)
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    return out.rename(columns=cols)
