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

    def band_at(self, ts) -> str:
        return self.hours[pd.Timestamp(ts).hour]

    def price_at(self, ts) -> float:
        return float(self.rates[self.band_at(ts)])

    def price_series(self, timestamps) -> pd.Series:
        idx = pd.DatetimeIndex(timestamps)
        return pd.Series([self.rates[self.hours[h]] for h in idx.hour], index=idx, dtype=float)


def _validate(tid: str, item: dict) -> Tariff:
    hours, rates = item.get("hours"), item.get("rates")
    if not isinstance(hours, list) or len(hours) != 24:
        raise TariffError(f"{tid}: hours는 24칸이어야 합니다")
    unknown = set(hours) - set(rates or {})
    if unknown:
        raise TariffError(f"{tid}: rates에 없는 구간명 {sorted(unknown)}")
    if any(float(v) < 0 for v in rates.values()):
        raise TariffError(f"{tid}: 음수 단가")
    return Tariff(tid, item.get("label", tid), bool(item.get("verified", False)), tuple(hours),
                  {k: float(v) for k, v in rates.items()})


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
