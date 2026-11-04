"""기상(기온)·특일(공휴일) 공개 데이터 수집 — 계획서 4.1 가 '외부환경: 기상, 요일, 시간대, 특일' 수집 항목.

- 기온 : 기상청 지상(종관, ASOS) 시간자료 — 관측소 기본값 156(광주). 전날까지의 관측값만 제공된다.
- 특일 : 한국천문연구원 특일 정보(getRestDeInfo) — 법정 공휴일·대체공휴일.

두 API 모두 공공데이터포털 인증키(`DATA_GO_KR_SERVICE_KEY`) 하나로 호출한다. 요청 주소·필드는 `config/public_apis.json`에
있고, 실제 키로 응답을 확인하기 전까지 `spec_verified=false`다. 받은 값은 `grid_signal`의 `temperature_c`·`is_holiday`를
덮어쓴다(시각·날짜가 맞는 행만). `is_holiday`는 기존 정의대로 주말이거나 공휴일이면 1이다.
"""
from __future__ import annotations

import os

import pandas as pd
import requests

from src.pipeline.public_api import ApiNotConfigured, ApiResponseError, ApiSpec, _number, load_spec, parse_items

WEATHER_API = "kma_asos_hourly"
HOLIDAY_API = "kasi_holidays"
DEFAULT_STATION = "156"   # 광주 — 실증 거점(광주·나주) 기준
MAX_PAGES = 50


def _key(spec: ApiSpec, service_key: str | None) -> str:
    key = service_key or os.environ.get(spec.key_env)
    if not key:
        raise ApiNotConfigured(f"{spec.key_env}가 설정되지 않았습니다. 공공데이터포털에서 '{spec.label}' 활용신청 후 인증키를 .env에 넣어 주세요.")
    if not spec.endpoint:
        raise ApiNotConfigured(f"{spec.api_id}의 엔드포인트가 비어 있습니다. config/public_apis.json을 채워 주세요.")
    return key


def _paged(spec: ApiSpec, query: dict, session, timeout: float, page_size: int) -> list[dict]:
    p, http, out, page = spec.params, session or requests, [], 1
    while True:
        q = {**query, p["page"]: page, p["rows"]: page_size}
        resp = http.get(spec.endpoint, params=q, timeout=timeout)
        resp.raise_for_status()
        items = parse_items(resp.content, spec)
        out.extend(items)
        if len(items) < page_size:
            return out
        page += 1
        if page > MAX_PAGES:
            raise ApiResponseError(f"페이지가 너무 많습니다({MAX_PAGES}쪽 초과) — 조회 기간을 줄여 주세요")


def fetch_hourly_temperature(start, end, station: str = DEFAULT_STATION, service_key: str | None = None,
                             session=None, timeout: float = 15.0, page_size: int = 999) -> pd.DataFrame:
    """[start, end] 날짜의 시간별 기온 → (timestamp, temperature_c). 결측(빈 값)은 버린다."""
    spec = load_spec(WEATHER_API)
    key = _key(spec, service_key)
    p = spec.params
    query = {p["key"]: key, p["format"]: "JSON", p["station"]: station,
             p["start_date"]: pd.Timestamp(start).strftime("%Y%m%d"), p["start_hour"]: "00",
             p["end_date"]: pd.Timestamp(end).strftime("%Y%m%d"), p["end_hour"]: "23", **p.get("fixed", {})}
    rows = []
    for it in _paged(spec, query, session, timeout, page_size):
        try:
            ts = pd.to_datetime(str(it[spec.fields["time"]]).strip())
        except (KeyError, ValueError) as exc:
            raise ApiResponseError(f"관측 시각을 해석할 수 없습니다(필드 매핑 확인 필요): {exc}") from exc
        rows.append({"timestamp": ts, "temperature_c": _number(it.get(spec.fields["temperature"]))})
    df = pd.DataFrame(rows, columns=["timestamp", "temperature_c"]).dropna()
    return df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)


def fetch_holidays(start, end, service_key: str | None = None, session=None, timeout: float = 15.0) -> pd.DataFrame:
    """[start, end]에 걸친 달의 공휴일 → (date, name). isHoliday='Y'인 날만."""
    spec = load_spec(HOLIDAY_API)
    key = _key(spec, service_key)
    p, f = spec.params, spec.fields
    rows = []
    for month in pd.period_range(pd.Timestamp(start), pd.Timestamp(end), freq="M"):
        query = {p["key"]: key, p["format"]: "json", p["year"]: f"{month.year}", p["month"]: f"{month.month:02d}"}
        for it in _paged(spec, query, session, timeout, 50):
            if str(it.get(f["is_holiday"], "Y")).strip().upper() != "Y":
                continue
            try:
                day = pd.to_datetime(str(it[f["date"]]).strip(), format="%Y%m%d").date()
            except (KeyError, ValueError) as exc:
                raise ApiResponseError(f"공휴일 날짜를 해석할 수 없습니다(필드 매핑 확인 필요): {exc}") from exc
            rows.append({"date": day, "name": str(it.get(f["name"], "")).strip()})
    return pd.DataFrame(rows, columns=["date", "name"]).drop_duplicates("date").reset_index(drop=True)


def merge_into_grid_signal(grid_df: pd.DataFrame, temperature_df: pd.DataFrame | None,
                           holidays_df: pd.DataFrame | None, start, end) -> tuple[pd.DataFrame, dict]:
    """시각이 맞는 행의 기온과, [start, end] 날짜 행의 공휴일 여부를 덮어쓴다. 나머지 열·기간은 그대로 둔다."""
    merged = grid_df.copy()
    ts = pd.to_datetime(merged["timestamp"])
    stats = {"temperature_rows": 0, "holiday_rows": 0, "holidays": []}
    if temperature_df is not None and not temperature_df.empty:
        temp = dict(zip(pd.to_datetime(temperature_df["timestamp"]), temperature_df["temperature_c"]))
        mask = ts.isin(temp)
        merged.loc[mask, "temperature_c"] = ts[mask].map(temp)
        stats["temperature_rows"] = int(mask.sum())
    if holidays_df is not None:
        days = set(holidays_df["date"])
        in_range = (ts.dt.date >= pd.Timestamp(start).date()) & (ts.dt.date <= pd.Timestamp(end).date())
        flag = ((ts.dt.dayofweek >= 5) | ts.dt.date.isin(days)).astype(int)
        merged.loc[in_range, "is_holiday"] = flag[in_range]
        stats["holiday_rows"] = int(in_range.sum())
        stats["holidays"] = [{"date": d.isoformat(), "name": n} for d, n in zip(holidays_df["date"], holidays_df["name"])]
    return merged, stats


def status() -> list[dict]:
    """키·명세 준비 상태(값은 노출하지 않음) — 연동 현황 표시용."""
    return [load_spec(a).status() for a in (WEATHER_API, HOLIDAY_API)]
