"""VPP·이익공유 실증 테스트 — 출력제어 해소 에뮬레이터(C01), 재원별 편익(S01), 모의 포인트 정산(E03), ESS 집중 실증 그룹(D03)."""
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api.routers import vpp
from src.auth import store as auth_store
from src.datagen.config import SimConfig
from src.optimizer import curtailment, ess_group
from src.settlement import ledger

client = TestClient(app)
CFG = SimConfig(n_ev=6, weeks=3, n_chargers=10, charger_days=21, n_zones=2, random_seed=7, start_date="2026-11-01")


@pytest.fixture(scope="module")
def small_bundle():
    return vpp.build_bundle(CFG)


@pytest.fixture
def admin(tmp_path, monkeypatch, small_bundle):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(vpp, "bundle", lambda: small_bundle)
    auth_store.create_user("admin-vpp", "admin-pass-1", "admin")
    assert client.post("/auth/login", json={"username": "admin-vpp", "password": "admin-pass-1"}).status_code == 200
    yield
    client.post("/auth/logout")


# ------------------------------------------------------------------ C01
def test_cleaning_removes_duplicates_negatives_and_outliers():
    ts = pd.date_range("2026-11-01", periods=24 * 7, freq="h")
    noise = np.random.default_rng(0).normal(0, 2, len(ts))
    g = pd.DataFrame({"timestamp": ts, "power_demand_mw": 80.0 + noise, "renewable_gen_mw": 10.0 + noise.clip(-5, 5),
                      "temperature_c": 15.0, "price_krw_per_kwh": 100.0})
    g.loc[5, "renewable_gen_mw"] = -3.0
    g.loc[30, "power_demand_mw"] = 9999.0
    g.loc[40, "temperature_c"] = None
    g = pd.concat([g, g.iloc[[10]]])
    clean, rep = curtailment.clean_public_series(g)
    assert rep["duplicates_removed"] == 1 and rep["negatives_zeroed"] == 1 and rep["missing_interpolated"] == 1
    assert clean["renewable_gen_mw"].min() >= 0
    assert clean["power_demand_mw"].max() < 9999


def test_emulator_reduces_curtailment_and_never_absorbs_more_than_surplus(small_bundle):
    c = small_bundle["curtailment"]
    t = c["totals"]
    assert t["curtail_after_kwh"] <= t["curtail_before_kwh"]
    h = c["_hourly"]
    assert (h["absorb_kw"] >= 0).all()
    assert t["absorbed_kwh"] <= t["curtail_before_kwh"] + 1e-6 or t["curtail_before_kwh"] == 0
    # 피크 방전은 흡수한 전력(효율 손실 후)을 넘지 않는다
    assert t["peak_discharge_kwh"] <= t["absorbed_kwh"] + 1e-6
    assert c["pipeline"][0] == "공공데이터 수집" and len(c["patterns"]["by_hour"]) == 24
    assert "_hourly" not in curtailment.public_view(c)


def test_revenue_sources_are_formula_based_not_fixed_ratios(small_bundle):
    rs = small_bundle["revenue_sources"]
    keys = [i["key"] for i in rs["items"]]
    assert keys == ["tou_arbitrage", "peak_reduction", "curtailment_relief"]
    assert rs["items"][0]["amount_krw"] == small_bundle["revenue"]["savings_krw"]
    assert rs["items"][2]["amount_krw"] == small_bundle["curtailment"]["totals"]["curtailment_benefit_krw"]
    shares = sorted(i["share_pct"] for i in rs["items"])
    assert shares != sorted([62.0, 25.0, 13.0])
    assert rs["verified"] is False and "가정" in rs["label"]


# ------------------------------------------------------------------ E03
def test_settlement_is_idempotent_and_balance_is_ledger_sum(small_bundle):
    trades = ledger.compute_trades(small_bundle["run"], small_bundle["ev_df"], small_bundle["curtailment"],
                                   small_bundle["params"], lambda e: ("public" if e.endswith("1") else "private", "테스트"))
    assert not trades.empty and (trades["points"] >= 0).all()
    first = ledger.settle(trades, "inferred", "synthetic")
    again = ledger.settle(trades, "inferred", "synthetic")
    assert first["inserted"] == len(trades) and again["inserted"] == 0 and again["skipped_already_settled"] == len(trades)
    ev = trades["ev_id"].iloc[0]
    assert ledger.balance(ev) == int(trades.loc[trades["ev_id"] == ev, "points"].sum())
    s = ledger.summary()
    assert {g["affiliation"] for g in s["groups"]} == {"public", "private"}
    assert s["total_points"] == int(trades["points"].sum())
    stmt = ledger.owner_statement(ev)
    assert stmt["balance_points"] == ledger.balance(ev) and stmt["history"][0]["generation_label"] == "추론(모의)"


def test_incentive_simulation_compares_three_types(small_bundle):
    trades = ledger.compute_trades(small_bundle["run"], small_bundle["ev_df"], small_bundle["curtailment"],
                                   small_bundle["params"], lambda e: ("private", "테스트"))
    sim = ledger.simulate_incentives(trades, small_bundle["params"])
    assert [o["type"] for o in sim["options"]] == ["points", "charge_discount", "revenue_share"]


def test_settlement_api_and_owner_points(admin, tmp_path, monkeypatch, small_bundle):
    r = client.post("/vpp/settlement/run")
    assert r.status_code == 200 and r.json()["inserted"] > 0
    assert client.post("/vpp/settlement/run").json()["inserted"] == 0          # 다시 돌려도 중복 적립 없음
    assert client.get("/vpp/settlement/summary").status_code == 200
    p = client.get("/vpp/settlement/preview").json()
    assert p["n_trades"] > 0 and len(p["incentives"]["options"]) == 3
    client.post("/auth/logout")
    ev_id = small_bundle["ev_df"]["ev_id"].iloc[0]
    auth_store.create_user("owner-vpp", "owner-pass-1", "owner", owned_ev_id=ev_id)
    assert client.post("/auth/login", json={"username": "owner-vpp", "password": "owner-pass-1"}).status_code == 200
    me = client.get("/owner/points")
    assert me.status_code == 200 and me.json()["ev_id"] == ev_id and me.json()["balance_points"] >= 0
    assert client.get("/vpp/settlement/summary").status_code == 403                # 차주는 전체 집계를 볼 수 없다


# ------------------------------------------------------------------ D03
def test_ess_candidates_scores_and_dataset(small_bundle):
    cands = ess_group.analyze_candidates(small_bundle["ev_df"], small_bundle["charger_df"])
    assert len(cands) == small_bundle["ev_df"]["ev_id"].nunique()
    assert cands == sorted(cands, key=lambda c: c["score"], reverse=True)
    assert all(0 <= c["score"] <= 100 for c in cands)
    assert 5 <= len(ess_group.recommend(cands, 99)) <= 10 or len(cands) < 5
    ds = ess_group.build_dataset(small_bundle["ev_df"], [cands[0]["ev_id"]])
    assert set(ds["ev_id"]) == {cands[0]["ev_id"]}
    assert (ds.loc[~ds["discharge_allowed"], "dischargeable_kwh"] == 0).all()
    assert (ds["generation"] == "inferred").all()


def test_ess_group_api_requires_5_to_10_and_exports_csv(admin, small_bundle):
    ids = list(small_bundle["ev_df"]["ev_id"].unique())
    assert client.put("/vpp/ess-group", json={"ev_ids": ids[:2]}).status_code == 422
    assert client.get("/vpp/ess-group/dataset.csv").status_code == 409             # 지정 전에는 내보낼 수 없다
    r = client.put("/vpp/ess-group", json={"ev_ids": ids[:5], "reason": "test"})
    assert r.status_code == 200 and len(r.json()["members"]) == 5
    assert client.put("/vpp/ess-group", json={"ev_ids": ids[:4] + ["NOPE-1"]}).status_code == 422
    g = client.get("/vpp/ess-group").json()
    assert len(g["members"]) == 5 and g["group_size_target"] == [5, 10]
    csv = client.get("/vpp/ess-group/dataset.csv")
    assert csv.status_code == 200 and "discharge_allowed" in csv.text


def test_curtailment_and_revenue_api(admin):
    c = client.get("/vpp/curtailment")
    assert c.status_code == 200 and "totals" in c.json() and "_hourly" not in c.json()
    assert client.get("/vpp/revenue-sources").json()["items"][0]["key"] == "tou_arbitrage"
