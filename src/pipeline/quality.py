"""데이터 품질 검증 — 결측치·범위·중복 체크 (WS1 "품질관리 기준", Y2 WS1 "이상치·결측치 처리")."""
from dataclasses import dataclass, field

import pandas as pd

from .schema import ColumnSpec


@dataclass
class QualityReport:
    table: str
    n_rows: int
    missing: dict = field(default_factory=dict)
    out_of_range: dict = field(default_factory=dict)
    duplicate_keys: int = 0

    @property
    def passed(self) -> bool:
        missing_ratio = sum(self.missing.values()) / max(self.n_rows, 1)
        return missing_ratio < 0.01 and self.duplicate_keys == 0 and sum(self.out_of_range.values()) == 0


def validate(df: pd.DataFrame, table: str, columns: list[ColumnSpec], dedup_keys: list[str] | None = None) -> QualityReport:
    report = QualityReport(table=table, n_rows=len(df))

    for col in columns:
        if col.name not in df.columns:
            if col.required:
                report.missing[col.name] = len(df)
            continue
        n_missing = int(df[col.name].isna().sum())
        if n_missing and col.required:
            report.missing[col.name] = n_missing

        if col.min_value is not None or col.max_value is not None:
            series = pd.to_numeric(df[col.name], errors="coerce")
            oob = 0
            if col.min_value is not None:
                oob += int((series < col.min_value).sum())
            if col.max_value is not None:
                oob += int((series > col.max_value).sum())
            if oob:
                report.out_of_range[col.name] = oob

    if dedup_keys:
        report.duplicate_keys = int(df.duplicated(subset=dedup_keys).sum())

    return report


def summarize(reports: list[QualityReport]) -> str:
    lines = ["| 테이블 | 건수 | 결측 | 범위위반 | 중복키 | 판정 |", "|---|---|---|---|---|---|"]
    for r in reports:
        status = "PASS" if r.passed else "FAIL"
        lines.append(
            f"| {r.table} | {r.n_rows:,} | {sum(r.missing.values())} | "
            f"{sum(r.out_of_range.values())} | {r.duplicate_keys} | {status} |"
        )
    return "\n".join(lines)
