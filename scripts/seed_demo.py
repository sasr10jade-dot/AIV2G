"""에뮬레이션 데모 데이터 채우기 — 충전사업자·요금·정산 정책이 확정되기 전에 화면과 흐름을 끝까지 보기 위한 가상 값.

  python scripts/seed_demo.py            # 채우기(여러 번 실행해도 중복 없음)
  python scripts/seed_demo.py --reset    # 이 스크립트가 만든 에뮬레이션 행만 지우기

만드는 것(모두 가상, 출처 표시 포함)
- 충전기 마스터 150기(사업자 CPO-A/CPO-B 코드), CPO 모의 어댑터 세션 동기화
- 방전 이벤트: V2G 지원 차량이 저녁 시간대에 방전한 것으로 모의해 기록(kind=V2G, source='emulator')
- 수집 동의(목적 collect/share_external/incentive, 버전 'demo-v0'), 실험군/대조군 배정
- 어드바이저리 몇 건(수락·거절 섞임)

에뮬레이션 방전 이벤트는 대시보드에서 공식 실적과 분리해 표시하며, 5,500건 산정에 합산하지 않는다.
실데이터가 들어오면 --reset으로 가상 행을 지우면 된다.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from src.advisory import service as adv  # noqa: E402
from src.cpo.adapters import MockAdapter  # noqa: E402
from src.cpo.sync import sync_all  # noqa: E402
from src.datagen.charger import build_charger_registry, generate_charger_sessions  # noqa: E402
from src.datagen.config import SimConfig  # noqa: E402
from src.events import discharge  # noqa: E402
from src.experiment import assign as exp_assign  # noqa: E402
from src.kpi import EMULATOR_SOURCE  # noqa: E402
from src.optimizer.economics import EconomicsParams  # noqa: E402
from src.pipeline import store  # noqa: E402
from src.pipeline.source import load_scenario  # noqa: E402
from src.privacy import consent  # noqa: E402

EXPERIMENT_ID = "demo-advisory-2026"
CONSENT_VERSION = "demo-v0"


def reset(con) -> dict:
    out = {
        "discharge_events": con.execute("DELETE FROM discharge_events WHERE source=?", (EMULATOR_SOURCE,)).rowcount,
        "advisories": con.execute("DELETE FROM advisories WHERE channel='api' AND inputs LIKE '%\"emulated\": true%'").rowcount,
        "experiment_groups": con.execute("DELETE FROM experiment_groups WHERE experiment_id=?", (EXPERIMENT_ID,)).rowcount,
        "cpo_sessions": con.execute("DELETE FROM charger_sessions WHERE source IN ('cpo_mock')").rowcount
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='charger_sessions'").fetchone() else 0,
    }
    con.commit()
    pc = consent.connect()
    out["consents"] = pc.execute("DELETE FROM consents WHERE version=?", (CONSENT_VERSION,)).rowcount
    pc.commit()
    pc.close()
    return out


def seed(con) -> dict:
    cfg = SimConfig()
    ev_df, _charger_df, grid_df, _real = load_scenario(cfg)
    rng = np.random.default_rng(cfg.random_seed)
    summary: dict = {}

    # 1) 충전기 마스터
    charger_df = generate_charger_sessions(cfg, np.random.default_rng(cfg.random_seed), ev_df)
    reg = build_charger_registry(cfg, charger_df)
    con.execute("DELETE FROM charger_registry")
    reg.to_sql("charger_registry", con, if_exists="append", index=False)
    summary["charger_registry"] = len(reg)

    # 2) CPO 모의 세션 동기화 (사업자 2곳)
    res = sync_all([MockAdapter("CPO-A", 75, 30, seed=1), MockAdapter("CPO-B", 75, 30, seed=2)], con=con)
    summary["cpo_sync"] = {r["operator_id"]: r["rows"] for r in res["results"]}

    # 3) 방전 이벤트 — V2G 지원 차량이 관측 후반 6주 동안 저녁(18~21시)에 방전한 것으로 모의한다.
    #    (합성 가격 스프레드가 작아 최적화기는 방전을 거의 내지 않으므로, '충전기가 방전했다'는 기록 자체를 모의)
    v2g_evs = sorted(ev_df[ev_df["v2g_capable"].astype(bool)]["ev_id"].unique()) if "v2g_capable" in ev_df else []
    last = ev_df["timestamp"].max().normalize()
    n_events = 0
    for ev_id in v2g_evs:
        for _ in range(int(rng.integers(3, 6))):
            day = last - __import__("pandas").Timedelta(days=int(rng.integers(1, 42)))
            start = day + __import__("pandas").Timedelta(hours=int(rng.integers(18, 21)))
            dur = float(rng.choice([1.0, 1.5, 2.0]))
            kwh = round(float(rng.uniform(3.0, 6.5)) * dur, 2)
            n_events += discharge.record_discharge_event(
                ev_id, start.isoformat(), dur, kwh, "V2G", None, EMULATOR_SOURCE, con)
    summary["discharge_events_new"] = n_events

    # 4) 동의 + 실험군 배정
    ev_models = ev_df.drop_duplicates("ev_id").set_index("ev_id")["vehicle_model"].to_dict()
    for ev_id in ev_models:
        for purpose in ("collect", "share_external", "incentive"):
            consent.grant(ev_id, purpose, CONSENT_VERSION, recorded_by="seed_demo")
    groups = exp_assign.assign(EXPERIMENT_ID, ev_models, 0.5, con=con)
    summary["experiment"] = {g: list(groups.values()).count(g) for g in ("treatment", "control")}

    # 5) 어드바이저리 — 이미 에뮬레이션 건이 있으면 새로 만들지 않는다
    has = con.execute("SELECT COUNT(*) FROM advisories WHERE inputs LIKE '%\"emulated\": true%'").fetchone()[0]
    if not has:
        eco = EconomicsParams.load()
        picker = random.Random(7)
        for ev_id in list(ev_models)[:12]:
            plan = {"ev_id": ev_id, "net_krw": float(rng.integers(300, 2500)), "assumptions": eco.as_dict()}
            aid = adv.issue(ev_id, plan, "api", inputs={"emulated": True}, group_id=groups.get(ev_id), con=con)
            status = picker.choice(["accepted", "accepted", "declined", None])
            if status:
                adv.respond(aid, status, con=con)
        summary["advisories_new"] = 12
    else:
        summary["advisories_new"] = 0
    con.commit()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset", action="store_true", help="이 스크립트가 만든 에뮬레이션 행만 삭제")
    args = ap.parse_args()
    con = store.get_connection()
    try:
        out = reset(con) if args.reset else seed(con)
    finally:
        con.close()
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
