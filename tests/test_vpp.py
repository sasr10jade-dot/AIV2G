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
                                   small_bundle["revenue_sources"], small_bundle["params"],
                                   lambda e: ("public" if e.endswith("1") else "private", "테스트"))
    assert not trades.empty and (trades["points"] >= 0).all()
    first = ledger.settle(trades, "inferred", "synthetic", small_bundle["params"])
    again = ledger.settle(trades, "inferred", "synthetic", small_bundle["params"])
    assert first["inserted"] == len(trades) and again["inserted"] == 0 and again["skipped_already_settled"] == len(trades)
    ev = trades["ev_id"].iloc[0]
    assert ledger.balance(ev) == int(trades.loc[trades["ev_id"] == ev, "points"].sum())
    s = ledger.summary(small_bundle["params"])
    assert {g["affiliation"] for g in s["groups"]} == {"public", "private"}
    assert s["total_points"] == int(trades["points"].sum())
    stmt = ledger.owner_statement(ev)
    assert stmt["balance_points"] == ledger.balance(ev) and stmt["history"][0]["generation_label"] == "추론(모의)"


def test_incentive_simulation_compares_three_types(small_bundle):
    trades = ledger.compute_trades(small_bundle["run"], small_bundle["ev_df"], small_bundle["curtailment"],
                                   small_bundle["revenue_sources"], small_bundle["params"], lambda e: ("private", "테스트"))
    sim = ledger.simulate_incentives(trades, small_bundle["params"])
    assert [o["type"] for o in sim["options"]] == ["points", "charge_discount", "revenue_share", "mileage"]


def test_settlement_api_and_owner_points(admin, tmp_path, monkeypatch, small_bundle):
    r = client.post("/vpp/settlement/run")
    assert r.status_code == 200 and r.json()["inserted"] > 0
    assert client.post("/vpp/settlement/run").json()["inserted"] == 0          # 다시 돌려도 중복 적립 없음
    assert client.get("/vpp/settlement/summary").status_code == 200
    p = client.get("/vpp/settlement/preview").json()
    assert p["n_trades"] > 0 and len(p["incentives"]["options"]) == 4 and "economics" in p
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


# ------------------------------------------------------------------ 재원 배분 · 정산 회차 · 동의 · 문의 · 미션
def _trades(b, consent_of=None):
    return ledger.compute_trades(b["run"], b["ev_df"], b["curtailment"], b["revenue_sources"], b["params"],
                                 lambda e: ("private", "테스트"), consent_of)


def test_points_come_from_revenue_source_pools(small_bundle):
    t = _trades(small_bundle)
    pools = {i["key"]: i["amount_krw"] for i in small_bundle["revenue_sources"]["items"]}
    assert abs(t["curtail_krw"].sum() - pools["curtailment_relief"]) <= len(t)          # 재원을 기여도로 나눈 합 = 재원(반올림 오차)
    assert abs(t["peak_krw"].sum() - pools["peak_reduction"]) <= len(t)
    share = small_bundle["params"]["points"]["owner_share_pct"] / 100
    assert abs(t["points"].sum() - t["value_krw"].sum() * share) <= len(t)
    econ = ledger.economics_check(t, small_bundle["params"])
    assert econ["payout_ratio_pct"] is not None and abs(econ["payout_ratio_pct"] - share * 100) < 1
    assert econ["judgement"] in ("참여 유인 충분", "참여 유인 부족 — 배분율·재원 확대 검토")


def test_discharge_needs_v2g_consent_for_registered_vehicles(small_bundle):
    ev0 = small_bundle["ev_df"]["ev_id"].iloc[0]
    no = _trades(small_bundle, lambda e: False if e == ev0 else None)
    mine = no[no["ev_id"] == ev0]
    assert (mine["peak_discharge_kwh"] == 0).all() and (mine["consent_note"] == "방전 미동의 — 방전 실적 제외").all()
    yes = _trades(small_bundle, lambda e: True if e == ev0 else None)
    assert yes.loc[yes["ev_id"] == ev0, "peak_discharge_kwh"].sum() >= mine["peak_discharge_kwh"].sum()


def test_run_lifecycle_draft_confirm_cancel(small_bundle):
    t = _trades(small_bundle)
    d0, d1 = t["trade_date"].min(), t["trade_date"].max()
    mid = sorted(t["trade_date"].unique())[len(t["trade_date"].unique()) // 2]
    run = ledger.create_run(t, d0, mid, small_bundle["params"], "admin")
    ev = t["ev_id"].iloc[0]
    assert run["status"] == "draft" and ledger.balance(ev) == 0                        # 확정 전에는 잔액에 없다
    assert ledger.owner_statement(ev)["pending_points"] >= 0
    with pytest.raises(ledger.SettlementError):
        ledger.cancel_run(run["run_id"], "admin", "")                                    # 취소 사유 필수
    ledger.cancel_run(run["run_id"], "admin", "단가 재검토")
    again = ledger.create_run(t, d0, mid, small_bundle["params"], "admin")              # 취소 후 같은 기간 재산정 가능
    assert again["totals"]["trades"] == run["totals"]["trades"]
    ok = ledger.confirm_run(again["run_id"], "admin")
    assert ok["status"] == "confirmed" and ledger.balance(ev) > 0
    with pytest.raises(ledger.SettlementError):
        ledger.cancel_run(again["run_id"], "admin", "취소")                              # 확정은 취소 불가
    overlap = ledger.create_run(t, d0, d1, small_bundle["params"], "admin")
    assert overlap["totals"]["skipped_already_settled"] == again["totals"]["trades"]     # 이미 정산한 날은 건너뜀
    with pytest.raises(ledger.SettlementError, match="새로 정산할 실적이 없습니다"):
        ledger.create_run(t, d0, d1, small_bundle["params"], "admin")                  # 빈 회차는 만들지 않는다


def test_inquiry_and_adjustment(small_bundle):
    q = ledger.create_inquiry("EV-X", "어제 포인트가 안 들어왔어요", "2026-11-10")
    assert ledger.list_inquiries("open")[0]["inquiry_id"] == q["inquiry_id"]
    ledger.resolve_inquiry(q["inquiry_id"], "확인 후 보정했습니다", "admin", 120)
    assert ledger.balance("EV-X") == 120 and ledger.list_inquiries("resolved")[0]["adjust_points"] == 120
    with pytest.raises(ledger.SettlementError):
        ledger.resolve_inquiry(q["inquiry_id"], "중복", "admin", 10)


def test_missions_progress_claim_once_and_admin_settings(tmp_path, monkeypatch):
    from src.privacy import consent
    from src.settlement import missions
    st = {m["id"]: m for m in missions.status_for("EV-M1")}
    assert "consent_v2g" not in st                                              # 방전 참여 켜기 미션은 삭제됨
    assert st["consent_location"]["completed"] is False
    with pytest.raises(missions.MissionError):
        missions.claim("EV-M1", "consent_location")
    consent.grant("EV-M1", "location", "v1", recorded_by="t")
    got = missions.claim("EV-M1", "consent_location")
    assert got["points"] == 100 and got["balance_points"] == 100 and got["level"]["name"] == "새싹"
    with pytest.raises(missions.MissionError, match="이미"):
        missions.claim("EV-M1", "consent_location")
    missions.update_setting("odometer_input", False, 50, "admin")
    assert "odometer_input" not in {m["id"] for m in missions.status_for("EV-M1")}
    ov = missions.admin_overview()
    assert next(m for m in ov["missions"] if m["id"] == "consent_location")["completions"] == 1
    assert missions.level_of(5000)["name"] == "실버" and missions.level_of(4999)["to_next"] == 1


def test_mission_and_reward_api(admin, small_bundle):
    r = client.post("/vpp/settlement/runs", json={})
    assert r.status_code == 200 and r.json()["status"] == "draft"
    rid = r.json()["run_id"]
    assert client.get(f"/vpp/settlement/runs/{rid}").json()["trades"]
    assert client.post(f"/vpp/settlement/runs/{rid}/confirm", json={}).json()["status"] == "confirmed"
    assert client.get("/vpp/missions").json()["missions"]
    assert client.put("/vpp/missions/survey_answer", json={"enabled": True, "points": 250}).json()["points"] == 250
    client.post("/auth/logout")
    ev_id = small_bundle["ev_df"]["ev_id"].iloc[0]
    auth_store.create_user("owner-mis", "owner-pass-1", "owner", owned_ev_id=ev_id)
    assert client.post("/auth/login", json={"username": "owner-mis", "password": "owner-pass-1"}).status_code == 200
    ms = client.get("/owner/missions").json()
    assert ms["level"]["name"] and any(m["id"] == "survey_answer" and m["points"] == 250 for m in ms["missions"])
    assert client.post("/owner/missions/consent_location/claim").status_code == 409              # 아직 미완료
    pts = client.get("/owner/points").json()
    assert pts["balance_points"] > 0 and "by_source_krw" in pts["history"][0] and pts["level"]["name"]
    assert client.post("/owner/points/inquiries", json={"message": "포인트 문의"}).status_code == 200


def test_curtailment_three_scenarios_review_opinion_2():
    """평가의견 2: S1 즉시 충전 / S2 흡수 / S3 흡수+피크 방전 — 흡수량·완화량·피크 감소·SOC 미달 지표."""
    from src.datagen.config import SimConfig
    from src.api.routers import vpp
    b = vpp.build_bundle(SimConfig(n_ev=10, weeks=3, n_chargers=30, charger_days=21, start_date="2026-07-01"))
    sc = b["curtailment"]["scenarios"]
    s1, s2, s3 = sc["rows"]
    assert [r["id"] for r in sc["rows"]] == ["S1", "S2", "S3"]
    assert s1["absorbed_kwh"] == 0 and s1["curtail_relief_kwh"] == 0
    assert s2["absorbed_kwh"] == s3["absorbed_kwh"] >= 0 and s2["peak_discharge_kwh"] == 0
    assert s3["peak_grid_kw"] <= s1["peak_grid_kw"] and s3["min_soc_violations"] >= 0


def test_ess_group_criteria_and_aggregate_review_opinion_13():
    """평가의견 13: 주차 8h 이상 주 3회 이상 우선·기본 8대, 플러그인 비중, 합산 용량·출력·시간대별 동시 가용 대수."""
    from src.datagen.config import SimConfig
    from src.api.routers import vpp
    from src.optimizer import ess_group
    b = vpp.build_bundle(SimConfig(n_ev=12, weeks=3, n_chargers=30, charger_days=21, start_date="2026-12-01"))
    c = ess_group.analyze_candidates(b["ev_df"], b["charger_df"])
    assert {"avg_dwell_h", "plugin_ratio", "long_parking_per_week", "meets_criteria"} <= set(c[0])
    r = ess_group.recommend(c)
    assert len(r) == ess_group.GROUP_DEFAULT == 8
    if any(x["meets_criteria"] for x in c):
        assert r[0]["meets_criteria"]
    a = ess_group.aggregate_capacity(b["ev_df"], [x["ev_id"] for x in r])
    assert len(a["by_hour"]) == 24 and a["summary"]["max_concurrent"] <= 8
    assert all(abs(h["power_kw"] - h["concurrent_available"] * ess_group.MAX_DISCHARGE_KW) <= 0.4 for h in a["by_hour"])   # 출력 = 동시 가용 대수 x 방전 출력(반올림 오차)


def test_monthly_settlement_and_institution_report_review_opinion_3(small_bundle):
    """평가의견 3: 월 1회 모의 정산 상태, 관용차량은 기관 단위 리포트(확정분만)."""
    trades = ledger.compute_trades(small_bundle["run"], small_bundle["ev_df"], small_bundle["curtailment"],
                                   small_bundle["revenue_sources"], small_bundle["params"],
                                   lambda e: ("public" if e.endswith("1") else "private", "테스트"))
    months = ledger.month_status(trades)
    assert months and all(m["status"] == "open" for m in months)
    ledger.settle(trades, "inferred", "synthetic", small_bundle["params"])
    assert all(m["status"] == "confirmed" for m in ledger.month_status(trades))
    rep = ledger.institution_report()
    pub = trades[trades["affiliation"] == "public"]
    assert rep["totals"]["vehicles"] == pub["ev_id"].nunique() and rep["totals"]["points"] == int(pub["points"].sum())
    m0 = months[0]["month"]
    assert all(r["month"] == m0 for r in ledger.institution_report(m0)["by_month"])


def test_monthly_settlement_endpoints(admin, small_bundle):
    months = client.get("/vpp/settlement/months").json()["months"]
    assert months
    m = months[0]["month"]
    r = client.post("/vpp/settlement/runs", json={"month": m})
    assert r.status_code == 200 and r.json()["period_from"] == f"{m}-01"
    csv = client.get(f"/vpp/settlement/institution-report?fmt=csv&month={m}")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert client.get("/vpp/settlement/institution-report").json()["scope"].startswith("관용차량")


def test_management_board_review_opinions(admin, small_bundle):
    """평가의견 5·6·8·10·11: 필수성과 5, 관리지표 7, 경제적 편익 4, 원천별 데이터, 레퍼런스 4, 보상 3단계."""
    r = client.get("/kpi/management")
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["mandatory"]) == 5 and len(d["management"]) == 7 and len(d["benefits"]) == 4
    assert len(d["references"]) == 4 and [c["stage"] for c in d["compensation"]] == [1, 2, 3]
    assert {"counted", "count", "source"} <= set(d["data_sources"][0])
    assert d["data_plan"]["total"] == 4615


def test_s3_discharge_respects_min_soc(small_bundle):
    s3 = small_bundle["curtailment"]["scenarios"]["rows"][2]
    assert s3["min_soc_violations"] == 0 and s3["violations_without_guard"] >= 0 and s3["safety_capped_kwh"] >= 0
