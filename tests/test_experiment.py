"""실험 배정·인센티브 정산·효과 분석 테스트."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.auth import store as auth_store
from src.events import discharge
from src.experiment import assign as ea
from src.experiment.analysis import difference_in_means
from src.experiment.incentive import IncentivePolicy, compute_incentive
from src.pipeline import migrations as m
from src.privacy import consent


@pytest.fixture(autouse=True)
def _users(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")


MODELS = {f"EV{i:02d}": ("A" if i < 16 else "B") for i in range(30)}


def test_assignment_is_balanced_stratified_and_reproducible():
    g = ea.plan_assignment("exp1", MODELS)
    assert g == ea.plan_assignment("exp1", MODELS)
    assert abs(sum(v == "treatment" for v in g.values()) - 15) <= 1
    for model in ("A", "B"):
        n = [g[e] for e, mo in MODELS.items() if mo == model]
        assert abs(n.count("treatment") - n.count("control")) <= 1
    assert g != ea.plan_assignment("exp2", MODELS)  # 실험 ID가 다르면 다른 배정
    with pytest.raises(ValueError):
        ea.plan_assignment("x", MODELS, 1.0)


def test_existing_assignments_are_never_changed():
    con = sqlite3.connect(":memory:")
    m.migrate(con)
    first = ea.assign("e", {"A1": "m", "A2": "m", "A3": "m", "A4": "m"}, con=con)
    later = ea.assign("e", {"A1": "m", "A2": "m", "A3": "m", "A4": "m", "A5": "m", "A6": "m"}, con=con)
    assert all(later[k] == v for k, v in first.items()) and len(later) == 6
    assert ea.group_of("e", "A5", con=con) == later["A5"] and ea.group_of("e", "none", con=con) is None


def _ev(ev, ts, kwh, kind="V2G"):
    return {"ev_id": ev, "start_ts": ts, "energy_kwh": kwh, "kind": kind}


def test_incentive_peak_bonus_cap_and_kind_filter():
    pol = IncentivePolicy(100, (18, 19), 1.5, 1000, 1.0, False)
    events = [_ev("A", "2026-09-01T19:00", 2.0), _ev("A", "2026-09-02T10:00", 2.0),
              _ev("A", "2026-09-03T10:00", 1.0, "TEST"), _ev("B", "2026-09-01T19:00", 20.0)]
    r = compute_incentive(events, pol)
    a = next(x for x in r["rows"] if x["ev_id"] == "A")
    assert a["points"] == 500.0  # 2*100*1.5 + 2*100
    b = next(x for x in r["rows"] if x["ev_id"] == "B")
    assert b["points_before_cap"] == 3000.0 and b["points"] == 1000.0 and r["cap_applied"]
    assert r["skipped"] == {"not_counted_kind": 1} and r["assumption"] is True
    only_a = compute_incentive(events, pol, eligible_evs={"A"})
    assert {x["ev_id"] for x in only_a["rows"]} == {"A"} and only_a["skipped"]["no_consent_or_not_eligible"] == 1


def test_default_policy_loads_unverified():
    p = IncentivePolicy.load()
    assert p.peak_multiplier >= 1 and not p.verified


def test_difference_in_means_ci_and_small_sample_guard():
    big = difference_in_means([10, 11, 12, 13, 14, 15], [1, 2, 3, 4, 5, 6])
    assert big["ok"] and big["significant"] and big["ci_low"] > 0
    null = difference_in_means([1, 5, 2, 6, 3, 4], [4, 2, 6, 1, 5, 3])
    assert null["ok"] and not null["significant"]
    assert difference_in_means([1, 2], [1, 2, 3])["ok"] is False


def test_experiment_api_requires_super_admin_and_incentive_respects_consent():
    auth_store.create_user("root", "root-password-1", "super_admin")
    auth_store.create_user("op", "op-password-1", "admin")
    discharge.record_discharge_event("EV-C", "2026-09-01T19:00", 1.0, 2.0, "V2G")
    discharge.record_discharge_event("EV-N", "2026-09-01T19:00", 1.0, 2.0, "V2G")
    consent.grant("EV-C", "incentive", "v1")

    op = TestClient(app)
    op.post("/auth/login", json={"username": "op", "password": "op-password-1"})
    assert op.get("/experiment/incentive").status_code == 403

    root = TestClient(app)
    root.post("/auth/login", json={"username": "root", "password": "root-password-1"})
    r = root.get("/experiment/incentive").json()
    assert {x["ev_id"] for x in r["rows"]} == {"EV-C"}
    a = root.post("/experiment/assign", json={"experiment_id": "e1", "ev_models": {"X1": "a", "X2": "a"}})
    assert a.status_code == 200 and set(a.json()["groups"].values()) == {"treatment", "control"}
    assert root.post("/experiment/assign", json={"experiment_id": "e1", "ev_models": {"X1": "a"}, "treatment_ratio": 2}).status_code == 422
