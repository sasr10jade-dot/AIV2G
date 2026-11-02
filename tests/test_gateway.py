"""LLM 게이트웨이 테스트 — 외부 호출 없이 가짜 call_fn으로 마스킹·한도·캐시·재시도·숫자검증·대체를 검증."""
import pytest

from src.copilot import gateway as g

PLAN = {
    "ev_id": "EV-TORRES-01", "departure_time": "2026-10-03T08:00:00",
    "charge_intervals": [{"time": "2026-10-03T02:00:00", "kw": 6.0}, {"time": "2026-10-03T03:00:00", "kw": 6.0}],
    "discharge_intervals": [{"time": "2026-10-02T19:00:00", "kw": 5.0}],
    "total_charge_kwh": 12.0, "total_discharge_kwh": 5.0, "net_krw": 880.0,
    "assumptions": {"verified": False},
}
DEMO = g.PROFILES["demo"]


def tmpl():
    return "TEMPLATE"


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(call, profile=DEMO, clock=None, sleeps=None):
    clock = clock or Clock()
    return g.LlmGateway(call, profile, clock=clock, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_masking_hides_plate_phone_email_and_pseudonymizes_ev_id():
    out, restore = g.mask_text("차량 43오3450 경기78아8416 EV-TORRES-01 연락 010-1234-5678 a@b.co")
    assert "43오3450" not in out and "8416" not in out and "5678" not in out and "a@b.co" not in out
    assert "EV-TORRES-01" not in out and len(restore) == 1
    assert g.unmask_text(next(iter(restore)), restore) == "EV-TORRES-01"


def test_prompt_sent_to_llm_is_masked_and_answer_is_unmasked():
    seen = {}

    def call(system, prompt):
        seen["prompt"] = prompt
        pid = [w for w in prompt.split() if w.startswith("PE-")][0]
        return f"{pid} 차량은 순이익 880원 예상입니다."

    r = make(call).generate("sys", "EV-TORRES-01 번호판 43오3450 계획", PLAN, tmpl)
    assert "EV-TORRES-01" not in seen["prompt"] and "43오3450" not in seen["prompt"]
    assert r.source == "llm" and r.text.startswith("EV-TORRES-01")


def test_unsupported_number_falls_back_to_template():
    r = make(lambda s, p: "순이익 9,999원 예상").generate("s", "p", PLAN, tmpl)
    assert (r.source, r.reason, r.text) == ("template", "unsupported_numbers", "TEMPLATE")
    ok = make(lambda s, p: "순이익 880원, 방전 5kWh, 충전 12kWh 예상").generate("s", "p", PLAN, tmpl)
    assert ok.source == "llm"
    derived = make(lambda s, p: "충전 12kW 구간").generate("s", "p", PLAN, tmpl, extra_allowed=(12.0,))
    assert derived.source == "llm"


def test_numbers_without_unit_and_times_are_not_checked():
    r = make(lambda s, p: "02시부터 04시까지 2회 충전하세요").generate("s", "p", PLAN, tmpl)
    assert r.source == "llm"


def test_cache_hit_avoids_second_call_and_expires():
    calls = []
    clock = Clock()
    gw = make(lambda s, p: calls.append(1) or "순이익 880원", clock=clock)
    assert gw.generate("s", "q", PLAN, tmpl).source == "llm"
    assert gw.generate("s", "q", PLAN, tmpl).source == "cache" and len(calls) == 1
    clock.t = DEMO.cache_ttl_s + 1
    assert gw.generate("s", "q", PLAN, tmpl).source == "llm" and len(calls) == 2


def test_per_minute_quota_then_refill():
    prof = g.Profile("t", True, per_minute=2, daily_cap=100, cache_ttl_s=0, max_retries=0)
    clock = Clock()
    gw = make(lambda s, p: "ok", prof, clock)
    assert [gw.generate("s", f"q{i}", PLAN, tmpl).source for i in range(3)] == ["llm", "llm", "template"]
    assert gw.generate("s", "q9", PLAN, tmpl).reason == "quota"
    clock.t = 31.0  # 30초에 1개 충전
    assert gw.generate("s", "q10", PLAN, tmpl).source == "llm"


def test_daily_cap():
    prof = g.Profile("t", True, per_minute=100, daily_cap=2, cache_ttl_s=0, max_retries=0)
    gw = make(lambda s, p: "ok", prof)
    assert [gw.generate("s", f"q{i}", PLAN, tmpl).source for i in range(3)] == ["llm", "llm", "template"]


def test_transient_errors_retry_with_backoff_then_succeed_or_fallback():
    state = {"n": 0}

    def flaky(s, p):
        state["n"] += 1
        if state["n"] < 3:
            raise g.TransientLlmError("429")
        return "순이익 880원"

    sleeps = []
    assert make(flaky, sleeps=sleeps).generate("s", "p", PLAN, tmpl).source == "llm"
    assert sleeps == [1, 2]

    def always(s, p):
        raise g.TransientLlmError("503")

    r = make(always).generate("s", "p2", PLAN, tmpl)
    assert (r.source, r.reason) == ("template", "error")


def test_non_transient_error_does_not_retry_and_falls_back():
    calls = []

    def bad(s, p):
        calls.append(1)
        raise RuntimeError("auth")

    assert make(bad).generate("s", "p", PLAN, tmpl).reason == "error" and len(calls) == 1


def test_local_profile_never_calls_llm():
    def boom(s, p):
        raise AssertionError("호출되면 안 됨")

    r = make(boom, g.PROFILES["local"]).generate("s", "p", PLAN, tmpl)
    assert (r.source, r.reason) == ("template", "local_profile")


def test_template_is_deterministic_and_flags_assumptions():
    t = g.template_next_session(PLAN)
    assert t == g.template_next_session(PLAN)
    assert "880원" in t and "08시" in t and "가정값" in t
    assert not g.unsupported_numbers(t, PLAN, extra_allowed=(12.0,))  # 템플릿 자체가 근거 안에 있다


def test_active_profile_from_env(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE", "local")
    assert g.active_profile().name == "local"
    monkeypatch.setenv("LLM_PROFILE", "nonsense")
    assert g.active_profile().name == "demo"


def test_service_local_profile_explain_returns_template_without_key(monkeypatch):
    from src.copilot import service

    monkeypatch.setenv("LLM_PROFILE", "local")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert "880원" in service.explain_next_session_plan(PLAN)
    with pytest.raises(service.CopilotNotConfigured):
        service.ask("질문")
