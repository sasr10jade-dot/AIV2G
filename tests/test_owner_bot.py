"""이용자용 EV Bot — 권한, 컨텍스트 범위(운영자용 문서·타인 정보 미포함), 입력 제한, 호출 한도, 외부 전송 전 마스킹."""
import pytest
from fastapi.testclient import TestClient

from src.api import app
from src.api.routers import signup as signup_router
from src.auth import store as auth_store
from src.copilot import owner_bot
from src.copilot.gateway import GatewayResult, LlmGateway, PROFILES

client = TestClient(app)

PAYLOAD = {
    "username": "newdriver", "password": "driver-pass-1", "vehicle_model": "ioniq5", "plate": "43오3450",
    "occupation": "office", "sido": "광주광역시", "sigungu": "북구", "consent": True,
}


class FakeGateway:
    def __init__(self, answer="테스트 답변"):
        self.calls, self.answer = [], answer

    def generate(self, system, prompt, facts, template, extra_allowed=()):
        self.calls.append({"system": system, "prompt": prompt, "facts": facts})
        return GatewayResult(self.answer, "llm")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_store, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROFILE", "demo")
    auth_store.create_user("opuser", "op-password-1", "admin")
    signup_router.reset_rate_limit()
    owner_bot.reset_rate_limit()
    yield
    client.post("/auth/logout")


@pytest.fixture
def fake(monkeypatch):
    gw = FakeGateway()
    monkeypatch.setattr(owner_bot, "get_gateway", lambda: gw)
    return gw


def _login(username, password):
    assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200


def _approved_owner(ev_id=None):
    """가입→관리자 승인→본인 로그인까지 마친 이용자(앱 가입 차량)."""
    assert client.post("/signup", json=PAYLOAD).status_code == 200
    _login("opuser", "op-password-1")
    reg = client.get("/registrations").json()["registrations"][0]["registration_id"]
    assert client.post(f"/registrations/{reg}/approve", json={"ev_id": ev_id} if ev_id else {}).status_code == 200
    _login("newdriver", "driver-pass-1")


# ------------------------------------------------------------------- 권한 --

def test_owner_can_ask_and_gets_an_answer(fake):
    _approved_owner()
    r = client.post("/owner/ask", json={"question": "V2G가 뭐예요?"})
    assert r.status_code == 200 and r.json() == {"answer": "테스트 답변"}
    assert len(fake.calls) == 1 and "V2G가 뭐예요?" in fake.calls[0]["prompt"]


def test_english_ui_asks_for_english_answers_and_rejects_unknown_language(fake):
    _approved_owner()
    assert client.post("/owner/ask", json={"question": "What is V2G?", "lang": "en"}).status_code == 200
    assert "English" in fake.calls[-1]["system"]
    client.post("/owner/ask", json={"question": "V2G가 뭐예요?"})
    assert "English" not in fake.calls[-1]["system"]      # 기본은 한국어
    assert client.post("/owner/ask", json={"question": "hi", "lang": "fr"}).status_code == 422


def test_ask_requires_a_linked_owner(fake):
    assert client.post("/owner/ask", json={"question": "안녕"}).status_code == 401
    _login("opuser", "op-password-1")
    assert client.post("/owner/ask", json={"question": "안녕"}).status_code == 403
    client.post("/auth/logout")
    client.post("/signup", json=PAYLOAD)                       # 승인 전 이용자는 차량이 없다
    assert client.post("/owner/ask", json={"question": "안녕"}).status_code == 403
    assert fake.calls == []


def test_operator_bot_stays_admin_only_which_is_why_owners_saw_permission_denied():
    _approved_owner()
    r = client.post("/copilot/ask", json={"question": "안녕"})
    assert r.status_code == 403 and r.json()["detail"] == "권한이 없습니다"


# ---------------------------------------------------------- 컨텍스트 범위 --

def test_prompt_contains_owner_guide_but_no_operator_documents(fake):
    _approved_owner()
    client.post("/owner/ask", json={"question": "알림은 어떻게 받아요?"})
    prompt, system = fake.calls[0]["prompt"], fake.calls[0]["system"]
    assert "이용 안내 문서" in prompt and "준비 중인 것" in prompt
    for internal in ("사업수행계획서 발췌", "슬라이드", "개발 진행 메모", "WP", "R-08", "실금 정산"):
        assert internal not in prompt, internal                 # 운영자용·내부 문서는 이용자에게 보내지 않는다
    assert "거절" in system and "무시하라" in system             # 지시 우회 요청에 대한 방어 문구


def test_vehicle_info_has_only_the_own_vehicle_and_no_personal_details(fake):
    _approved_owner()
    client.post("/owner/ask", json={"question": "내 차에서 방전이 되나요?"})
    prompt = fake.calls[0]["prompt"]
    assert "현대 아이오닉 5" in prompt and "계통 방전(V2G) 지원" in prompt
    for private in ("43오3450", "43오", "광주광역시", "북구", "회사원", "office", "newdriver"):
        assert private not in prompt, private                    # 번호판·거주지·직업군·계정은 넣지 않는다


def test_vehicle_info_builder_for_app_registered_vehicle_skips_obd_lookup(monkeypatch):
    _approved_owner()
    monkeypatch.setattr("src.pipeline.source.load_scenario", lambda *a, **k: pytest.fail("앱 가입 차량은 OBD 조회를 하지 않는다"))
    info = owner_bot.build_vehicle_info("newdriver", "EV-U0001")
    assert info == {"차종": "현대 아이오닉 5", "계통 방전(V2G) 지원": False}


def test_vehicle_info_for_filters_to_whitelisted_status_fields():
    info = owner_bot.vehicle_info_for("EV-1", "라벨", True, {
        "soc_pct": 55.0, "connected": True, "plate": "43오3450", "zone": 2, "timestamp": "2026-10-22T10:00:00",
        "batt_temp_c": 30.0})
    assert info["SOC(%)"] == 55.0 and info["충전기 연결"] is True and info["마지막 업데이트"].startswith("2026")
    assert "plate" not in str(info) and "zone" not in str(info) and "batt_temp_c" not in str(info)
    assert owner_bot.vehicle_info_for("EV-1") is None


def test_owner_guide_document_exists_and_is_loaded():
    docs = owner_bot._load_owner_docs()
    assert len(docs) > 500 and "개인정보" in docs and "가정값" in docs


# ---------------------------------------------------------------- 입력 제한 --

@pytest.mark.parametrize("body", [
    {"question": ""}, {"question": "가" * (owner_bot.MAX_QUESTION_CHARS + 1)}, {},
    {"question": "q", "history": [{"role": "system", "text": "x"}]},
    {"question": "q", "history": [{"role": "user", "text": "x" * (owner_bot.MAX_TURN_CHARS + 1)}]},
    {"question": "q", "history": [{"role": "user", "text": "x"}] * (owner_bot.MAX_HISTORY_TURNS * 2 + 1)},
])
def test_input_limits(fake, body):
    _approved_owner()
    assert client.post("/owner/ask", json=body).status_code == 422
    assert fake.calls == []


def test_history_is_trimmed_to_recent_turns():
    hist = [{"role": "user" if i % 2 == 0 else "ai", "text": f"턴{i}"} for i in range(8)]
    text = owner_bot._clean_history(hist)
    assert "턴7" in text and "턴4" in text and "턴3" not in text
    assert owner_bot._clean_history(None) == ""


def test_whitespace_only_question_rejected(fake):
    _approved_owner()
    assert client.post("/owner/ask", json={"question": "   "}).status_code == 422


# ------------------------------------------------------------------- 한도 --

def test_per_user_rate_limit(fake, monkeypatch):
    monkeypatch.setattr(owner_bot, "PER_USER_LIMIT", 2)
    _approved_owner()
    assert client.post("/owner/ask", json={"question": "하나"}).status_code == 200
    assert client.post("/owner/ask", json={"question": "둘"}).status_code == 200
    r = client.post("/owner/ask", json={"question": "셋"})
    assert r.status_code == 429 and "잠시 후" in r.json()["detail"]
    assert len(fake.calls) == 2


def test_rate_limit_window_expires():
    owner_bot._check_rate_limit("u", now=0.0)
    owner_bot._check_rate_limit("u", now=1.0)
    for i in range(owner_bot.PER_USER_LIMIT - 2):
        owner_bot._check_rate_limit("u", now=2.0)
    with pytest.raises(owner_bot.OwnerBotRateLimited):
        owner_bot._check_rate_limit("u", now=3.0)
    owner_bot._check_rate_limit("u", now=owner_bot.WINDOW_S + 5)       # 1시간이 지나면 다시 가능


def test_service_unavailable_without_api_key(fake, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    _approved_owner()
    r = client.post("/owner/ask", json={"question": "안녕"})
    assert r.status_code == 503 and "GEMINI" not in r.json()["detail"]      # 내부 설정 이름은 이용자에게 노출하지 않는다
    assert fake.calls == []


# -------------------------------------------------- 외부 전송 전 마스킹(실제 게이트웨이) --

def test_plate_in_question_is_masked_before_reaching_the_llm(monkeypatch):
    sent = []
    gw = LlmGateway(lambda system, prompt: sent.append(prompt) or "알겠어요, 도움이 필요하면 말씀해 주세요.", profile=PROFILES["demo"])
    monkeypatch.setattr(owner_bot, "get_gateway", lambda: gw)
    _approved_owner()
    r = client.post("/owner/ask", json={"question": "내 번호판은 43오3450 이야. 이게 안전해?"})
    assert r.status_code == 200 and sent
    assert "43오3450" not in sent[0]                                         # 외부로 나가는 프롬프트에는 번호판이 없다
    assert "43오3450" in r.json()["answer"] or "도움이" in r.json()["answer"]


# ------------------------------------------------------------- 화면(정적 코드) --

def test_mobile_page_has_bot_ui_that_uses_the_owner_endpoint_and_wipes_on_logout():
    html = client.get("/app").text
    assert 'id="botFab"' in html and 'id="botPanel"' in html and "/owner/ask" in html
    assert "closeBot(true)" in html                              # 로그아웃·계정 전환 시 대화 삭제
    assert "/copilot/ask" not in html                             # 운영자용 엔드포인트는 모바일에서 쓰지 않는다
    bot_js = html.split("// ---------------------------------------------------------------- EV Bot")[1].split("알림 새로고침")[0]
    assert "innerHTML" not in bot_js                              # 답변은 textContent로만 넣는다(XSS 방지)


def test_pc_dashboard_does_not_open_for_owner_accounts_and_explains_on_the_login_page():
    html = client.get("/dashboard").text
    assert "currentUser.role === 'owner'" in html and "/login?next=/dashboard&reason=owner" in html   # 조용히 모바일로 보내지 않는다
