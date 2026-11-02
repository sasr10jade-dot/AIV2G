"""EV 40대 실증 인프라의 운행·충전·SOC 텔레메트리 합성 생성 (WS1).

시간당 1행으로 EV별 운행/충전 상태를 시뮬레이션한다. `ev_type`(운행 패턴: 출퇴근형/
업무용 Fleet/장시간 주차형)과 `vehicle_model`(차종·배터리 스펙·V2G 지원여부)은 서로
독립적인 두 축이다 — 실제로 어떤 차량이든(승용/업무용 상관없이) 특정 차종일 수 있다.

차종 5종을 두되 그 중 2종만 `v2g_capable=True`다 — 2026~27년 시점 국내 EV 시장에서
"그리드 연계 양방향 V2G"를 표준 지원하는 모델은 극소수이고(대부분은 V2L만 지원),
5개 완성차 제조사 모두와 방전 실증 협의를 끝내기는 현실적으로 어렵다는 판단에 따른
설계다 — 데이터 제공(5종)과 실제 방전 실증(그 중 일부)을 분리해서 모델링한다.
V2G 미지원 차종은 optimizer가 SOC/가격 조건과 무관하게 방전을 0으로 강제한다
(`src/optimizer/decision.py`의 `v2g_capable` 게이트).

중요: 각 시간대의 soc_pct는 반드시 "그 시간대 행동을 적용하기 이전" 값을 기록한다
(주행/충전 이후 값을 기록하면 세션 시작 시점 SOC가 실제보다 부풀려져 최적화 여지가
사라지는 버그가 생긴다 — tests/test_datagen.py::test_soc_recorded_before_action_not_after 참고).
"""
import numpy as np
import pandas as pd

from .config import SimConfig

EV_TYPES = ["승용형(출퇴근)", "업무용 Fleet", "장시간 주차형"]
EV_TYPE_WEIGHTS = [0.5, 0.3, 0.2]

# 차종 5종 — 배터리 용량은 차종 스펙, v2g_capable=False면 V2L(비상전원)만 가능하고
# 그리드 연계 방전(V2G)은 아직 지원하지 않는 것으로 간주한다.
VEHICLE_MODELS = {
    "차종A(세단)": {"battery_capacity_kwh": 77.4, "v2g_capable": True},
    "차종B(SUV)": {"battery_capacity_kwh": 84.0, "v2g_capable": True},
    "차종C(준중형)": {"battery_capacity_kwh": 58.0, "v2g_capable": False},
    "차종D(경형)": {"battery_capacity_kwh": 42.0, "v2g_capable": False},
    "차종E(픽업)": {"battery_capacity_kwh": 91.0, "v2g_capable": False},
}
VEHICLE_MODEL_WEIGHTS = [0.30, 0.25, 0.20, 0.15, 0.10]  # V2G 지원 2종(A/B)에 실증 우선순위 가중

TYPE_PARAMS = {
    "승용형(출퇴근)": {
        "weekday_drive_hours": (7, 8, 17, 18),
        "weekday_drive_prob": 0.9,
        "weekend_drive_hours": (11, 12, 15, 16),
        "weekend_drive_prob": 0.2,
        "day_connect_prob": 0.85,
        "night_connect_prob": 0.98,
        "consumption_range": (4.5, 7.0),
    },
    "업무용 Fleet": {
        "weekday_drive_hours": tuple(range(8, 18)),
        "weekday_drive_prob": 0.35,
        "weekend_drive_hours": tuple(range(9, 15)),
        "weekend_drive_prob": 0.1,
        "day_connect_prob": 0.25,
        "night_connect_prob": 0.95,
        "consumption_range": (5.0, 9.0),
    },
    "장시간 주차형": {
        "weekday_drive_hours": tuple(range(24)),
        "weekday_drive_prob": 0.03,
        "weekend_drive_hours": tuple(range(24)),
        "weekend_drive_prob": 0.02,
        "day_connect_prob": 0.97,
        "night_connect_prob": 0.99,
        "consumption_range": (4.5, 6.0),
    },
}

MAX_CHARGE_KW = 7.0  # 완속충전기 평균 출력 (charger.py SESSION_KW과 동일 가정)
TARGET_SOC_PCT = 90.0  # 이 이상은 배터리 보호를 위해 충전하지 않음
NIGHT_HOURS = frozenset(set(range(0, 6)) | set(range(20, 24)))


def generate_ev_telemetry(cfg: SimConfig, rng: np.random.Generator) -> pd.DataFrame:
    ev_types = rng.choice(EV_TYPES, size=cfg.n_ev, p=EV_TYPE_WEIGHTS)
    vehicle_models = rng.choice(list(VEHICLE_MODELS.keys()), size=cfg.n_ev, p=VEHICLE_MODEL_WEIGHTS)
    ev_zones = rng.integers(0, cfg.n_zones, size=cfg.n_ev)
    ts_start = pd.Timestamp(cfg.start_date)

    rows = []
    for i in range(cfg.n_ev):
        ev_id = f"EV-{i + 1:03d}"
        ev_type = str(ev_types[i])
        vehicle_model = str(vehicle_models[i])
        model_spec = VEHICLE_MODELS[vehicle_model]
        battery_capacity_kwh = model_spec["battery_capacity_kwh"]
        v2g_capable = model_spec["v2g_capable"]
        zone = int(ev_zones[i])
        params = TYPE_PARAMS[ev_type]
        soc = float(rng.uniform(40.0, 90.0))

        for day in range(cfg.n_days):
            date = ts_start + pd.Timedelta(days=day)
            is_weekend = date.dayofweek >= 5
            drive_hours = params["weekend_drive_hours"] if is_weekend else params["weekday_drive_hours"]
            drive_prob = params["weekend_drive_prob"] if is_weekend else params["weekday_drive_prob"]

            for hour in range(24):
                ts = date + pd.Timedelta(hours=hour)
                soc_at_start = soc  # 이 시간대 행동 이전 값 — 그대로 기록한다

                driving = bool(hour in drive_hours and rng.random() < drive_prob)
                connected = False
                charging_kw = 0.0

                if driving:
                    consumption_kwh = rng.uniform(*params["consumption_range"])
                    soc = max(soc_at_start - consumption_kwh / battery_capacity_kwh * 100, 0.0)
                    # 실측(OBDData 분석) 기준 방향성 재현: 주행 중(방전 전류 큼) 발열이 가장 크다.
                    batt_temp_c = rng.uniform(20.0, 28.0) + consumption_kwh * 2.0
                else:
                    connect_prob = params["night_connect_prob"] if hour in NIGHT_HOURS else params["day_connect_prob"]
                    connected = bool(rng.random() < connect_prob)
                    if connected and soc_at_start < TARGET_SOC_PCT:
                        need_kwh = (TARGET_SOC_PCT - soc_at_start) / 100 * battery_capacity_kwh
                        charging_kw = float(min(MAX_CHARGE_KW, need_kwh))
                        soc = min(soc_at_start + charging_kw / battery_capacity_kwh * 100, TARGET_SOC_PCT)
                        batt_temp_c = rng.uniform(18.0, 24.0) + charging_kw * 1.5
                    else:
                        soc = soc_at_start
                        batt_temp_c = rng.uniform(15.0, 22.0)
                batt_temp_c = round(min(batt_temp_c, 65.0), 1)

                rows.append(
                    (
                        ts, ev_id, ev_type, zone,
                        round(soc_at_start, 2), connected, driving, round(charging_kw, 2),
                        vehicle_model, battery_capacity_kwh, v2g_capable, batt_temp_c,
                    )
                )

    df = pd.DataFrame(
        rows,
        columns=[
            "timestamp", "ev_id", "ev_type", "zone", "soc_pct", "connected", "driving", "charging_kw",
            "vehicle_model", "battery_capacity_kwh", "v2g_capable", "batt_temp_c",
        ],
    )
    return df.sort_values(["ev_id", "timestamp"]).reset_index(drop=True)
