"""수익 계산 가정값 — 열화비용·충방전 효율·방전 횟수 상한을 한곳에서 관리한다.

순이익 = 방전수익 - 충전비용 - 열화비용
  방전수익 = 계통에 내보낸 kWh x 방전 시점 단가
  충전비용 = 계통에서 산 kWh x 충전 시점 단가
  열화비용 = 배터리에서 꺼낸 kWh(= 방전 kWh / 방전효율) x 열화단가(원/kWh)

열화단가는 (배터리 팩 단가 / (사이클 수 x 사용 DoD))로 근사한다 — 계산식은
`derive_degradation_krw_per_kwh`. 값은 config/economics.json에서 읽고, 확정 근거가 없는
동안은 verified=false로 두어 "가정값" 표기를 강제한다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from src.paths import PROJECT_ROOT

ECONOMICS_CONFIG = PROJECT_ROOT / "config" / "economics.json"


def derive_degradation_krw_per_kwh(pack_cost_krw_per_kwh: float, cycle_life: float, usable_dod: float = 1.0) -> float:
    """배터리 1kWh를 통과시킬 때 소모되는 수명 비용(원/kWh)."""
    if pack_cost_krw_per_kwh < 0 or cycle_life <= 0 or not 0 < usable_dod <= 1:
        raise ValueError("팩 단가>=0, 사이클 수>0, 0<DoD<=1 이어야 합니다")
    return pack_cost_krw_per_kwh / (cycle_life * usable_dod)


def breakeven_sell_price(buy_krw_per_kwh: float, economics: "EconomicsParams") -> float:
    """방전이 이득이 되는 최소 보상단가(원/kWh, 계통으로 내보낸 kWh 기준).

    계통에 1kWh를 내보내려면 1/(eta_c x eta_d) kWh를 사서 충전해야 하고, 배터리에서 1/eta_d kWh를
    꺼내므로 그만큼 열화비용이 든다: s* = b/(eta_c x eta_d) + 열화단가/eta_d.
    """
    eta_c, eta_d = economics.charge_efficiency, economics.discharge_efficiency
    return buy_krw_per_kwh / (eta_c * eta_d) + economics.degradation_krw_per_kwh / eta_d


@dataclass(frozen=True)
class EconomicsParams:
    charge_efficiency: float = 1.0      # 계통 kWh -> 배터리 kWh
    discharge_efficiency: float = 1.0   # 배터리 kWh -> 계통 kWh
    degradation_krw_per_kwh: float = 0.0
    max_discharge_events_per_day: int | None = None  # None이면 제한 없음
    verified: bool = False              # False면 결과를 '가정값'으로 표기해야 한다

    def __post_init__(self) -> None:
        for name in ("charge_efficiency", "discharge_efficiency"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name}는 0 초과 1 이하여야 합니다")
        if self.degradation_krw_per_kwh < 0:
            raise ValueError("열화단가는 음수일 수 없습니다")
        if self.max_discharge_events_per_day is not None and self.max_discharge_events_per_day < 0:
            raise ValueError("방전 횟수 상한은 음수일 수 없습니다")

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: Path | None = None) -> "EconomicsParams":
        p = Path(path or ECONOMICS_CONFIG)
        if not p.exists():
            return cls()
        cfg = json.loads(p.read_text(encoding="utf-8"))
        deg = cfg.get("degradation_krw_per_kwh")
        if deg is None and "pack_cost_krw_per_kwh" in cfg:
            deg = derive_degradation_krw_per_kwh(
                cfg["pack_cost_krw_per_kwh"], cfg["cycle_life"], cfg.get("usable_dod", 1.0))
        return cls(
            charge_efficiency=cfg.get("charge_efficiency", 1.0),
            discharge_efficiency=cfg.get("discharge_efficiency", 1.0),
            degradation_krw_per_kwh=float(deg or 0.0),
            max_discharge_events_per_day=cfg.get("max_discharge_events_per_day"),
            verified=bool(cfg.get("verified", False)),
        )
