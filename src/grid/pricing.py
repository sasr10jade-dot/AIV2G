"""차주 관점 가격 모델(FR-P01·P02) — 충전 단가(buy)와 방전 보상단가(sell)를 분리한다.

기존 최적화는 grid_signal의 `price_krw_per_kwh` 하나로 충전 비용과 방전 수익을 모두 계산했다.
실제 차주는 충전사업자·한전 요금표 단가로 사고(공공 완속 295원/kWh 수준), 방전 보상은 제도가
없어 SMP·DR·계약 등 가정 시나리오로만 둘 수 있다. 같은 가격을 쓰면 차익이 과대평가되므로
두 가격을 따로 들고 다니는 `PriceModel`을 만든다.

- buy: `config/tariffs.json`의 요금표(시각별 단가)
- sell: `config/compensation.json`의 보상 시나리오(zero / grid_signal / flat, 선택적 DR 프리미엄)

DR 프리미엄은 방전 보상(sell)에만 곱한다 — DR은 부하를 줄이거나 내보낸 쪽을 보상하는
제도이지 충전 요금을 올리는 제도가 아니다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from src.grid.dr_events import apply_dr_premium_to_price
from src.grid.tariff import Tariff, TariffError, get_tariff
from src.paths import PROJECT_ROOT

COMPENSATION_CONFIG = PROJECT_ROOT / "config" / "compensation.json"
COMPENSATION_KINDS = ("zero", "grid_signal", "flat")

DEFAULT_TARIFF_ID = "kepco_ev_example"
DEFAULT_COMPENSATION_ID = "smp"


@dataclass(frozen=True)
class Compensation:
    compensation_id: str
    label: str
    kind: str
    rate: float
    apply_dr: bool
    verified: bool


def _validate(cid: str, item: dict) -> Compensation:
    kind = item.get("kind")
    if kind not in COMPENSATION_KINDS:
        raise TariffError(f"{cid}: kind는 {COMPENSATION_KINDS} 중 하나여야 합니다")
    rate = float(item.get("rate", 0.0))
    if kind == "flat" and "rate" not in item:
        raise TariffError(f"{cid}: flat 보상은 rate가 필요합니다")
    if rate < 0:
        raise TariffError(f"{cid}: 음수 보상단가")
    return Compensation(cid, item.get("label", cid), kind, rate,
                        bool(item.get("apply_dr", False)), bool(item.get("verified", False)))


def load_compensations(path: Path | None = None) -> dict[str, Compensation]:
    cfg = json.loads(Path(path or COMPENSATION_CONFIG).read_text(encoding="utf-8"))
    return {cid: _validate(cid, item) for cid, item in cfg.get("compensations", {}).items()}


def get_compensation(compensation_id: str, path: Path | None = None) -> Compensation:
    comps = load_compensations(path)
    if compensation_id not in comps:
        raise TariffError(f"알 수 없는 보상 시나리오: {compensation_id} (등록: {sorted(comps)})")
    return comps[compensation_id]


@dataclass(frozen=True)
class PriceModel:
    buy_by_time: dict
    sell_by_time: dict
    tariff: Tariff
    compensation: Compensation

    @property
    def verified(self) -> bool:
        """둘 다 확정값일 때만 True — 하나라도 가정이면 결과 전체가 '가정값'이다."""
        return self.tariff.verified and self.compensation.verified

    def describe(self) -> dict:
        return {
            "tariff_id": self.tariff.tariff_id,
            "tariff_label": self.tariff.label,
            "compensation_id": self.compensation.compensation_id,
            "compensation_label": self.compensation.label,
            "verified": self.verified,
        }


def build_price_model(
    timestamps,
    grid_price_by_time: dict,
    tariff_id: str = DEFAULT_TARIFF_ID,
    compensation_id: str = DEFAULT_COMPENSATION_ID,
    tariff_path: Path | None = None,
    compensation_path: Path | None = None,
) -> PriceModel:
    """`grid_price_by_time`은 DR 프리미엄을 적용하기 전의 grid_signal 가격이다(grid_signal 보상에만 쓰임)."""
    tariff = get_tariff(tariff_id, tariff_path)
    comp = get_compensation(compensation_id, compensation_path)
    times = [pd.Timestamp(t) for t in timestamps]

    buy = {t: tariff.price_at(t) for t in times}
    if comp.kind == "zero":
        sell = {t: 0.0 for t in times}
    elif comp.kind == "grid_signal":
        sell = {t: float(grid_price_by_time.get(t, 0.0)) for t in times}
    else:
        sell = {t: comp.rate for t in times}
    if comp.apply_dr:
        sell = apply_dr_premium_to_price(sell)
    return PriceModel(buy, sell, tariff, comp)


def price_model_for_grid(grid_df: pd.DataFrame, tariff_id: str = DEFAULT_TARIFF_ID,
                         compensation_id: str = DEFAULT_COMPENSATION_ID) -> PriceModel:
    grid_price = dict(zip(grid_df["timestamp"], grid_df["price_krw_per_kwh"]))
    return build_price_model(grid_df["timestamp"], grid_price, tariff_id, compensation_id)
