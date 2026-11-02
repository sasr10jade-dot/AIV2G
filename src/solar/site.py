"""태양광 연계 충전소 사이트 설정 — config/solar_sites.json을 읽어 검증한다.

사이트 값은 코드가 아니라 설정이다. 실제 설비 제원이 나오면 JSON만 교체하고 `verified`를 true로 바꾼다.
`verified=false`인 사이트로 계산한 결과는 '가정값(모의)'이므로 호출 측이 이 플래그를 보고 표기해야 한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.paths import PROJECT_ROOT

SOLAR_SITES_CONFIG = PROJECT_ROOT / "config" / "solar_sites.json"


class SiteError(ValueError):
    pass


@dataclass(frozen=True)
class DemandProfile:
    sessions_weekday: int = 24
    sessions_weekend: int = 14
    mean_kwh: float = 14.0
    sd_kwh: float = 6.0
    min_kwh: float = 3.0
    max_kwh: float = 40.0


@dataclass(frozen=True)
class SolarSite:
    site_id: str
    label: str
    verified: bool
    lat: float
    lon: float
    tz_offset_h: float
    kwp: float
    performance_ratio: float
    temp_coeff_per_c: float
    poa_gain: float
    n_chargers: int
    charger_kw: float
    grid_import_limit_kw: float
    tariff_id: str
    pv_price_krw_per_kwh: float
    demand: DemandProfile = field(default_factory=DemandProfile)

    @property
    def max_ev_load_kw(self) -> float:
        return self.n_chargers * self.charger_kw

    def public_dict(self) -> dict:
        return {
            "site_id": self.site_id, "label": self.label, "verified": self.verified,
            "lat": self.lat, "lon": self.lon, "pv_kwp": self.kwp, "n_chargers": self.n_chargers,
            "charger_kw": self.charger_kw, "grid_import_limit_kw": self.grid_import_limit_kw,
            "tariff_id": self.tariff_id, "pv_price_krw_per_kwh": self.pv_price_krw_per_kwh,
        }


def _positive(site_id: str, name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or value <= 0:
        raise SiteError(f"{site_id}: {name}는 0보다 커야 합니다")
    return float(value)


def _build(site_id: str, item: dict) -> SolarSite:
    pv, ch, dem = item.get("pv", {}), item.get("chargers", {}), item.get("demand", {})
    lat, lon = item.get("lat"), item.get("lon")
    if not isinstance(lat, (int, float)) or not -90 <= lat <= 90:
        raise SiteError(f"{site_id}: lat은 -90~90이어야 합니다")
    if not isinstance(lon, (int, float)) or not -180 <= lon <= 180:
        raise SiteError(f"{site_id}: lon은 -180~180이어야 합니다")
    pr = _positive(site_id, "pv.performance_ratio", pv.get("performance_ratio"))
    if pr > 1:
        raise SiteError(f"{site_id}: pv.performance_ratio는 1 이하여야 합니다")
    gamma = pv.get("temp_coeff_per_c", -0.004)
    if not isinstance(gamma, (int, float)) or not -0.02 <= gamma <= 0:
        raise SiteError(f"{site_id}: pv.temp_coeff_per_c는 -0.02~0 이어야 합니다")
    count = ch.get("count")
    if not isinstance(count, int) or count < 1:
        raise SiteError(f"{site_id}: chargers.count는 1 이상 정수여야 합니다")
    demand = DemandProfile(**{k: dem[k] for k in dem if k in DemandProfile.__dataclass_fields__})
    if demand.min_kwh <= 0 or demand.max_kwh < demand.min_kwh:
        raise SiteError(f"{site_id}: demand의 kWh 범위가 올바르지 않습니다")
    return SolarSite(
        site_id=site_id, label=item.get("label", site_id), verified=bool(item.get("verified", False)),
        lat=float(lat), lon=float(lon), tz_offset_h=float(item.get("tz_offset_h", 9)),
        kwp=_positive(site_id, "pv.kwp", pv.get("kwp")), performance_ratio=pr, temp_coeff_per_c=float(gamma),
        poa_gain=_positive(site_id, "pv.poa_gain", pv.get("poa_gain", 1.0)),
        n_chargers=count, charger_kw=_positive(site_id, "chargers.kw", ch.get("kw")),
        grid_import_limit_kw=_positive(site_id, "grid_import_limit_kw", item.get("grid_import_limit_kw")),
        tariff_id=item.get("tariff_id", "kepco_ev_example"),
        pv_price_krw_per_kwh=float(item.get("pv_price_krw_per_kwh", 0.0)), demand=demand,
    )


def load_sites(path: Path | None = None) -> dict[str, SolarSite]:
    cfg = json.loads(Path(path or SOLAR_SITES_CONFIG).read_text(encoding="utf-8"))
    return {sid: _build(sid, item) for sid, item in cfg.get("sites", {}).items()}


def get_site(site_id: str, path: Path | None = None) -> SolarSite:
    sites = load_sites(path)
    if site_id not in sites:
        raise SiteError(f"알 수 없는 사이트: {site_id} (등록: {sorted(sites)})")
    return sites[site_id]
