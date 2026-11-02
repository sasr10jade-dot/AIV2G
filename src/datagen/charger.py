"""충전기 150기 실증 인프라의 세션(이용) 로그 합성 생성.

산출근거(150기 × 147일 × 0.2 ≈ 4,410건)를 그대로 세션 발생률로 사용해,
생성 건수가 성과지표 목표치와 자연히 정합하도록 한다.
"""
import numpy as np
import pandas as pd

from .config import SimConfig

SESSION_KW = 7.0  # 완속충전기 평균 출력
AVG_SESSION_HOURS = 2.5

# 충전기 계측(AC) 대비 배터리 실충전(DC) 비율 — 실제 완속충전기 스펙값이 아니라,
# "차량-충전기 데이터를 비교해 효율을 추정할 수 있는가"(충전 효율 예측 기능)를
# 검증하기 위해 의도적으로 합성 데이터에 넣은 손실률이다. ev_id가 매칭된 세션의
# energy_kwh는 그 차량의 실제 충전량을 이 값으로 나눠 만들므로, 분석 쪽에서
# 역산하면 이 값 근처가 나와야 정상이다.
CHARGER_EFFICIENCY = 0.92


# 충전사업자는 협의 중이라 실명 대신 코드(CPO-A/B)로 배정한다 — 확정 시 config로 교체.
OPERATOR_IDS = ("CPO-A", "CPO-B")


def operator_for_charger(charger_idx: int) -> str:
    return OPERATOR_IDS[charger_idx % len(OPERATOR_IDS)]


def _build_ev_charging_lookup(ev_df: pd.DataFrame) -> dict[tuple, list[tuple]]:
    """(zone, 정시hour) -> [(ev_id, 그 시간 순간충전kW), ...] — 세션에 그럴듯한
    ev_id를 배정할 때, 같은 시간·구역에서 실제로 충전 중이던 차량 중 하나를 고른다."""
    charging = ev_df[(ev_df["connected"]) & (ev_df["charging_kw"] > 0.01)].copy()
    if charging.empty:
        return {}
    charging["_hour"] = charging["timestamp"].dt.floor("h")
    lookup: dict[tuple, list[tuple]] = {}
    for (zone, hour), grp in charging.groupby(["zone", "_hour"]):
        lookup[(int(zone), hour)] = list(zip(grp["ev_id"], grp["charging_kw"]))
    return lookup


def generate_charger_sessions(
    cfg: SimConfig, rng: np.random.Generator, ev_df: pd.DataFrame | None = None
) -> pd.DataFrame:
    """`ev_df`가 주어지면, 같은 시간·구역에 실제로 충전 중이던 차량이 있을 때
    그 차량의 `ev_id`를 세션에 배정하고 `energy_kwh`도 그 차량의 실제 충전량 기반
    (CHARGER_EFFICIENCY로 나눈 값)으로 계산한다 — 매칭되면 두 데이터셋이 같은
    실체(충전 사건)를 가리키게 되어 충전 효율 분석이 의미를 갖는다. 매칭되는
    차량이 없으면(또는 ev_df 미제공) 기존처럼 완전 독립적인 무작위값을 쓰고
    `ev_id`는 비워둔다 — 세션 발생 건수(PRD 산출근거)는 이 매칭 여부와 무관하게
    그대로 유지된다."""
    ts_start = pd.Timestamp(cfg.start_date)
    ev_lookup = _build_ev_charging_lookup(ev_df) if ev_df is not None and not ev_df.empty else {}
    rows = []
    session_id = 1

    for charger_idx in range(cfg.n_chargers):
        charger_id = f"CHG-{charger_idx + 1:04d}"
        zone = int(rng.integers(0, cfg.n_zones))
        operator_id = operator_for_charger(charger_idx)
        for day in range(cfg.charger_days):
            n_sessions_today = rng.poisson(cfg.charger_avg_sessions_per_day)
            for _ in range(n_sessions_today):
                start_hour = rng.uniform(0, 24)
                start_ts = ts_start + pd.Timedelta(days=day, hours=start_hour)
                duration_h = max(rng.normal(AVG_SESSION_HOURS, 0.8), 0.3)

                candidates = ev_lookup.get((zone, start_ts.floor("h")))
                if candidates:
                    ev_id, ev_charging_kw = candidates[rng.integers(0, len(candidates))]
                    energy_kwh = round(float(ev_charging_kw) * duration_h / CHARGER_EFFICIENCY, 2)
                else:
                    ev_id = None
                    energy_kwh = round(SESSION_KW * duration_h * rng.uniform(0.7, 1.0), 2)

                rows.append(
                    (
                        f"SESS-{session_id:06d}", charger_id, zone,
                        start_ts, round(duration_h, 2), energy_kwh, ev_id, operator_id, "synthetic",
                    )
                )
                session_id += 1

    df = pd.DataFrame(
        rows,
        columns=["session_id", "charger_id", "zone", "start_ts", "duration_h", "energy_kwh", "ev_id",
                 "operator_id", "source"],
    )
    return df.sort_values("start_ts").reset_index(drop=True)


def build_charger_registry(cfg: SimConfig, charger_df: pd.DataFrame) -> pd.DataFrame:
    """세션 로그에서 충전기 마스터(charger_registry)를 만든다 — 실데이터 연계 전 합성 단계용.
    사업자 실명은 협의 중이라 operator_name은 비워 둔다."""
    rows = []
    zone_of = charger_df.drop_duplicates("charger_id").set_index("charger_id")["zone"].to_dict() if not charger_df.empty else {}
    for idx in range(cfg.n_chargers):
        cid = f"CHG-{idx + 1:04d}"
        rows.append((cid, operator_for_charger(idx), None, None, int(zone_of.get(cid, 0)), "AC7", SESSION_KW, 0, 1))
    return pd.DataFrame(rows, columns=[
        "charger_id", "operator_id", "operator_name", "site_id", "zone",
        "connector_type", "max_kw", "v2g_capable", "active",
    ])
