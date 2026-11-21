"""계시별(TOU) 충전 요금표 — config/tariffs.json을 읽어 시각별 단가를 돌려준다.

요금표는 코드가 아니라 설정이다. 확정 요금표가 나오면 JSON만 교체하고 `verified`를 true로
바꾼다. `verified=false`인 요금표로 계산한 금액은 "가정값"이므로 호출 측이 이 플래그를 보고
화면·보고서에 표기해야 한다(`Tariff.verified`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.paths import PROJECT_ROOT

TARIFF_CONFIG = PROJECT_ROOT / "config" / "tariffs.json"


class TariffError(ValueError):
    pass


@dataclass(frozen=True)
class Tariff:
    tariff_id: str
    label: str
    verified: bool
    hours: tuple[str, ...]
    rates: dict[str, float]
    # 계절별 시간대·단가(한전 EV 충전전력요금처럼 여름·봄가을·겨울로 다른 경우). 없으면 hours·rates를 연중 쓴다.
    seasons: tuple = ()   # ((months, hours, rates, name), ...)
    # 요일 규칙(한전 계시별): 토요일은 구간명 치환(예: peak→mid), 일요일·공휴일은 하루 종일 한 구간(예: light).
    saturday_map: tuple = ()          # (("peak", "mid"), ...)
    sunday_band: str | None = None
    holidays: frozenset = frozenset()  # "YYYY-MM-DD"
    # 할인: ((months, day_types, hours, factor, name), ...) — day_types ⊂ {"weekday","saturday","sunday_holiday"}
    discounts: tuple = ()

    def _season(self, month: int):
        for months, hours, rates, name in self.seasons:
            if month in months:
                return hours, rates, name
        return self.hours, self.rates, None

    def day_type(self, ts, holiday: bool | None = None) -> str:
        t = pd.Timestamp(ts)
        if holiday or t.weekday() == 6 or t.strftime("%Y-%m-%d") in self.holidays:
            return "sunday_holiday"
        return "saturday" if t.weekday() == 5 else "weekday"

    def band_at(self, ts, holiday: bool | None = None) -> str:
        t = pd.Timestamp(ts)
        band = self._season(t.month)[0][t.hour]
        dt = self.day_type(t, holiday)
        if dt == "sunday_holiday" and self.sunday_band:
            return self.sunday_band
        if dt == "saturday":
            return dict(self.saturday_map).get(band, band)
        return band

    def season_at(self, ts) -> str | None:
        return self._season(pd.Timestamp(ts).month)[2]

    def discount_at(self, ts, holiday: bool | None = None) -> tuple[float, str | None]:
        t = pd.Timestamp(ts)
        dt = self.day_type(t, holiday)
        for months, day_types, hours, factor, name in self.discounts:
            if t.month in months and dt in day_types and t.hour in hours:
                return factor, name
        return 1.0, None

    def price_at(self, ts, holiday: bool | None = None) -> float:
        t = pd.Timestamp(ts)
        rates = self._season(t.month)[1]
        return float(rates[self.band_at(t, holiday)]) * self.discount_at(t, holiday)[0]

    def price_series(self, timestamps, holidays=None) -> pd.Series:
        """holidays: 시각별 공휴일 여부(같은 길이의 bool 열, 예: grid_signal.is_holiday). 없으면 요일·설정 공휴일만."""
        idx = pd.DatetimeIndex(timestamps)
        hol = [None] * len(idx) if holidays is None else [bool(h) for h in holidays]
        return pd.Series([self.price_at(t, h) for t, h in zip(idx, hol)], index=idx, dtype=float)


def _validate(tid: str, item: dict) -> Tariff:
    hours, rates = item.get("hours"), item.get("rates")
    if not isinstance(hours, list) or len(hours) != 24:
        raise TariffError(f"{tid}: hours는 24칸이어야 합니다")
    unknown = set(hours) - set(rates or {})
    if unknown:
        raise TariffError(f"{tid}: rates에 없는 구간명 {sorted(unknown)}")
    if any(float(v) < 0 for v in rates.values()):
        raise TariffError(f"{tid}: 음수 단가")
    seasons = []
    for s in item.get("seasons", []) or []:
        s_hours, s_rates = s.get("hours"), s.get("rates", rates)
        if not isinstance(s_hours, list) or len(s_hours) != 24:
            raise TariffError(f"{tid}/{s.get('name')}: 계절 hours는 24칸이어야 합니다")
        if set(s_hours) - set(s_rates):
            raise TariffError(f"{tid}/{s.get('name')}: rates에 없는 구간명")
        if any(float(v) < 0 for v in s_rates.values()):
            raise TariffError(f"{tid}/{s.get('name')}: 음수 단가")
        seasons.append((tuple(int(m) for m in s["months"]), tuple(s_hours), {k: float(v) for k, v in s_rates.items()}, s.get("name")))
    all_bands = set(rates) | {b for _, _, r, _ in seasons for b in r}
    days = item.get("day_rules", {}) or {}
    sat = tuple((str(k), str(v)) for k, v in (days.get("saturday_map") or {}).items())
    sun = days.get("sunday_holiday_band")
    if any(v not in all_bands for _, v in sat) or (sun and sun not in all_bands):
        raise TariffError(f"{tid}: day_rules에 없는 구간명")
    discounts = []
    for d in item.get("discounts", []) or []:
        factor = float(d.get("factor", 1.0))
        if not 0 <= factor <= 1:
            raise TariffError(f"{tid}/{d.get('name')}: 할인 factor는 0~1")
        day_types = frozenset(d.get("day_types", []))
        if day_types - {"weekday", "saturday", "sunday_holiday"}:
            raise TariffError(f"{tid}/{d.get('name')}: day_types 오류")
        discounts.append((tuple(int(m) for m in d["months"]), day_types, frozenset(int(h) for h in d["hours"]), factor, d.get("name")))
    return Tariff(tid, item.get("label", tid), bool(item.get("verified", False)), tuple(hours),
                  {k: float(v) for k, v in rates.items()}, tuple(seasons), sat, sun,
                  frozenset(days.get("holidays", []) or []), tuple(discounts))


def load_tariffs(path: Path | None = None) -> dict[str, Tariff]:
    cfg = json.loads(Path(path or TARIFF_CONFIG).read_text(encoding="utf-8"))
    return {tid: _validate(tid, item) for tid, item in cfg.get("tariffs", {}).items()}


def get_tariff(tariff_id: str, path: Path | None = None) -> Tariff:
    tariffs = load_tariffs(path)
    if tariff_id not in tariffs:
        raise TariffError(f"알 수 없는 요금표: {tariff_id} (등록: {sorted(tariffs)})")
    return tariffs[tariff_id]


def tariff_for_charger_type(charger_type: str, path: Path | None = None) -> Tariff:
    """충전기 유형(home/apartment/public_slow …)에 적용되는 요금표 — 공공은 출력 구간별 단일 단가,
    자가·아파트는 계시별처럼 유형마다 요금 체계가 다르므로 매핑은 설정(`charger_type_tariffs`)에 둔다."""
    cfg = json.loads(Path(path or TARIFF_CONFIG).read_text(encoding="utf-8"))
    mapping = cfg.get("charger_type_tariffs", {})
    if charger_type not in mapping:
        raise TariffError(f"요금표가 지정되지 않은 충전기 유형: {charger_type} (등록: {sorted(mapping)})")
    return get_tariff(mapping[charger_type], path)
