"""공개 데이터 CSV 반영 — 시간별 전력수요·재생에너지 발전량(계획서 4.1 가 '시간대별 전력수요 및 계통 관련 데이터, 재생에너지 관련 데이터').

한국전력거래소 자료는 공공데이터포털에서 시간별 파일(CSV)로 받는 경우가 많고, 파일마다 열 이름과 모양이 다르다.
그래서 열 이름을 고정하지 않고 두 가지 모양을 알아서 읽는다.
- 가로형: 날짜 열 + 1~24시 열(예: '날짜, 1시, 2시, …, 24시')            → 1시 = 00~01시 구간(시작 시각 00:00)
- 세로형: 날짜 열 + 시간 열 + 값 열(예: '거래일자, 거래시간, 지역, 연료원, 전력거래량(MWh)')
세로형에 지역 열이 있으면 region으로 거를 수 있고, 같은 시각의 값(연료원별 등)은 합친다.
값은 단위를 바꾸지 않고 그대로 넣는다(전국 수요는 도시 규모보다 훨씬 크다). 피크 시간대는 날짜별 상위 25%라 규모와 무관하다.
"""
from __future__ import annotations

import io
import re

import pandas as pd

KINDS = {"power-demand": "power_demand_mw", "renewable": "renewable_gen_mw"}
DATE_HINTS = ("날짜", "일자", "거래일", "기준일", "date", "ymd")
HOUR_HINTS = ("시간", "거래시간", "hour", "시각")
VALUE_HINTS = ("수요", "발전", "거래량", "mwh", "mw", "value", "값", "전력")
REGION_HINTS = ("지역", "시도", "region")
_HOUR_COL = re.compile(r"^\s*(\d{1,2})\s*(시|h|hr|:00)?\s*$", re.I)


class CsvFormatError(ValueError):
    """CSV 모양을 해석할 수 없음."""


def _read(content: bytes) -> pd.DataFrame:
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    raise CsvFormatError("CSV를 읽을 수 없습니다(UTF-8 또는 CP949 형식인지 확인해 주세요)")


def _find(cols, hints) -> str | None:
    for c in cols:
        low = str(c).strip().lower()
        if any(h in low for h in hints):
            return c
    return None


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.astype(str).str.replace(",", "").str.strip(), errors="coerce")


def _dates(s: pd.Series) -> pd.Series:
    d = pd.to_datetime(s.astype(str).str.strip(), format="%Y%m%d", errors="coerce")
    return d.fillna(pd.to_datetime(s.astype(str).str.strip(), format="mixed", errors="coerce")).dt.normalize()


def parse_hourly(content: bytes, region: str | None = None) -> pd.DataFrame:
    """CSV → (timestamp, value). 시작 시각 기준 한국시간, 결측은 버린다."""
    df = _read(content)
    df.columns = [str(c).strip() for c in df.columns]
    date_col = _find(df.columns, DATE_HINTS)
    if not date_col:
        raise CsvFormatError("날짜 열을 찾지 못했습니다(열 이름에 '날짜'·'일자'·'거래일' 등이 있어야 합니다)")
    hour_cols = {c: int(_HOUR_COL.match(c).group(1)) for c in df.columns if _HOUR_COL.match(c)}
    if len(hour_cols) >= 23:                                            # 가로형
        hours = sorted(hour_cols.values())
        base = 1 if hours[0] == 1 else 0                                 # 1~24시 표기면 1시 = 00시 시작
        long = df.melt(id_vars=[date_col], value_vars=list(hour_cols), var_name="_h", value_name="_v")
        long["timestamp"] = _dates(long[date_col]) + pd.to_timedelta(long["_h"].map(hour_cols) - base, unit="h")
        long["value"] = _num(long["_v"])
    else:                                                               # 세로형
        hour_col, region_col = _find(df.columns, HOUR_HINTS), _find(df.columns, REGION_HINTS)
        value_col = _find([c for c in df.columns if c not in (date_col, hour_col, region_col)], VALUE_HINTS)
        if not hour_col or not value_col:
            raise CsvFormatError("시간 열과 값 열을 찾지 못했습니다(가로형은 1~24시 열, 세로형은 '시간'·'발전량/수요' 열이 필요합니다)")
        if region and region_col:
            df = df[df[region_col].astype(str).str.contains(region, na=False)]
            if df.empty:
                raise CsvFormatError(f"'{region}' 지역 행이 없습니다")
        h = _num(df[hour_col].astype(str).str.extract(r"(\d{1,2})")[0])
        base = 0 if (h == 0).any() else 1                                # 0시가 없으면 1~24시 표기(KPX 거래시간)로 본다
        long = pd.DataFrame({"timestamp": _dates(df[date_col]) + pd.to_timedelta(h - base, unit="h"), "value": _num(df[value_col])})
    out = long.dropna(subset=["timestamp", "value"]).groupby("timestamp", as_index=False)["value"].sum()
    if out.empty:
        raise CsvFormatError("읽은 값이 없습니다")
    return out.sort_values("timestamp").reset_index(drop=True)


def merge_into_grid(grid_df: pd.DataFrame, series: pd.DataFrame, column: str) -> tuple[pd.DataFrame, int]:
    """시각이 맞는 행의 column만 덮어쓴다. 반환: (새 grid, 반영한 행 수)."""
    merged = grid_df.copy()
    ts = pd.to_datetime(merged["timestamp"])
    values = dict(zip(pd.to_datetime(series["timestamp"]), series["value"]))
    mask = ts.isin(values)
    merged.loc[mask, column] = ts[mask].map(values)
    return merged, int(mask.sum())
