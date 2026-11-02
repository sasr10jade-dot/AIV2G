"""차량(OBD) 배터리 충전 데이터와 충전기 계측 데이터를 비교해 충전 효율(손실률)을
추정한다 — "차량 배터리 상태와 충전기 데이터를 비교해서 에너지 효율을 예측할 수
없을까?"라는 요청에 대한 구현.

**전제 조건과 한계**: `charger_sessions`에 `ev_id`가 연결된 세션만 분석 대상이다.
합성 데이터는 `src/datagen/charger.py`가 같은 시간·구역에서 실제로 충전 중이던
차량을 찾아 `ev_id`를 배정하고, 그 세션의 `energy_kwh`도 그 차량의 실제 충전량을
`CHARGER_EFFICIENCY`(0.92)로 나눠 계산한다 — 그래서 이 분석이 역산한 평균 효율이
그 값 근처로 나오면 "차량-충전기 데이터를 연결해 효율을 추정하는 방법 자체가
동작한다"는 검증이 된다. 실측 데이터에서는 OCPP `id_tag`와 `ev_id`가 매핑돼야
채워진다(아직 그 매핑이 없음 — 별도 작업 필요).

두 값(충전기 계측 kWh, 차량 SOC 기반 추정 kWh) 다 세션 시작 시각의 순간 충전율
(kW)이 세션 내내 일정하다고 가정한 근사치라, 실제 충방전 곡선의 변동은 반영하지
못한다.
"""
import pandas as pd


class NoLinkedSessionsError(ValueError):
    """charger_sessions에 ev_id가 연결된 세션이 하나도 없을 때(실증 초기 등)."""


def compute_charging_efficiency(ev_df: pd.DataFrame, charger_df: pd.DataFrame) -> dict:
    if "ev_id" not in charger_df.columns:
        raise NoLinkedSessionsError(
            "charger_sessions에 ev_id 컬럼이 없습니다 — 차량-충전기 연결 정보가 없어 효율을 계산할 수 없습니다."
        )

    linked = charger_df[charger_df["ev_id"].notna()].copy()
    if linked.empty:
        raise NoLinkedSessionsError("ev_id가 연결된 충전 세션이 없습니다.")

    ev = ev_df.copy()
    ev["hour_bucket"] = ev["timestamp"].dt.floor("h")
    ev_charging_kw_by_key = {(r.ev_id, r.hour_bucket): r.charging_kw for r in ev.itertuples()}

    sessions: list[dict] = []
    for _, s in linked.iterrows():
        hour = pd.Timestamp(s["start_ts"]).floor("h")
        ev_kw = ev_charging_kw_by_key.get((s["ev_id"], hour))
        if ev_kw is None or ev_kw <= 0:
            continue
        charger_energy_kwh = float(s["energy_kwh"])
        if charger_energy_kwh <= 0:
            continue
        ev_energy_kwh = float(ev_kw) * float(s["duration_h"])

        sessions.append({
            "session_id": s["session_id"],
            "ev_id": s["ev_id"],
            "charger_id": s["charger_id"],
            "start_ts": pd.Timestamp(s["start_ts"]).isoformat(),
            "ev_side_energy_kwh": round(ev_energy_kwh, 2),
            "charger_side_energy_kwh": round(charger_energy_kwh, 2),
            "efficiency_pct": round(ev_energy_kwh / charger_energy_kwh * 100, 1),
        })

    if not sessions:
        raise NoLinkedSessionsError("ev_id가 연결된 세션은 있지만, 겹치는 시간대의 유효한 차량 충전 데이터가 없습니다.")

    avg_efficiency_pct = round(sum(s["efficiency_pct"] for s in sessions) / len(sessions), 1)
    # 에너지 가중 효율 = 전체 차량 쪽 에너지 ÷ 전체 충전기 쪽 에너지. 세션별 효율의 단순 평균은 작은 세션의 계측 오차를
    # 큰 세션과 같은 비중으로 반영해 전체 효율을 왜곡하므로, 대표값으로는 가중 효율을 쓰는 것이 맞다(단순 평균은 호환용으로 유지).
    total_charger = sum(s["charger_side_energy_kwh"] for s in sessions)
    weighted_efficiency_pct = round(sum(s["ev_side_energy_kwh"] for s in sessions) / total_charger * 100, 1) if total_charger else None
    return {
        "n_sessions_analyzed": len(sessions),
        "avg_efficiency_pct": avg_efficiency_pct,
        "weighted_efficiency_pct": weighted_efficiency_pct,
        "sessions": sessions,
        "note": (
            "두 값 다 세션 시작 시각의 순간 충전율이 세션 내내 유지된다고 가정한 근사치입니다. "
            "실측 데이터에서는 OCPP id_tag-ev_id 매핑이 확보돼야 세션이 연결됩니다."
        ),
    }
