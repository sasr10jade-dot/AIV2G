"""사용자 모바일웹(PWA) 껍데기 — 매니페스트·아이콘·서비스 워커·화면 서빙, 개인 데이터 비캐시."""
import json
import re
import struct

import pytest
from fastapi.testclient import TestClient

from src.api import app

client = TestClient(app)


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "PNG가 아님"
    return struct.unpack(">II", data[16:24])


def test_app_page_served_without_login_and_links_manifest():
    r = client.get("/app")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"
    html = r.text
    assert '<link rel="manifest" href="/manifest.webmanifest"' in html
    assert 'rel="apple-touch-icon"' in html
    assert 'name="viewport"' in html and "viewport-fit=cover" in html
    assert "/sw.js" in html


def test_app_page_contains_no_personal_data():
    """로그인 전 껍데기라 어떤 차량·계정 정보도 들어 있으면 안 된다(서비스 워커가 캐시하는 대상)."""
    html = client.get("/app").text
    assert not re.search(r"EV-\d", html)


def test_manifest_is_valid_and_installable():
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    for key in ("name", "short_name", "start_url", "scope", "display", "icons", "background_color", "theme_color"):
        assert m.get(key), f"manifest.{key} 누락"
    assert m["display"] == "standalone" and m["start_url"] == "/app"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])


def test_every_manifest_icon_is_served_with_declared_size():
    for icon in client.get("/manifest.webmanifest").json()["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.headers["content-type"] == "image/png"
        w, h = (int(x) for x in icon["sizes"].split("x"))
        assert _png_size(r.content) == (w, h), icon["src"]
    assert _png_size(client.get("/pwa/apple-touch-icon.png").content) == (180, 180)


@pytest.mark.parametrize("name", ["../owner.html", "..%2Fowner.html", "sw.js", "manifest.webmanifest", "nope.png"])
def test_icon_route_serves_only_whitelisted_files(name):
    assert client.get(f"/pwa/{name}").status_code == 404


def test_service_worker_scope_and_freshness():
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["service-worker-allowed"] == "/"


def test_service_worker_never_caches_personal_api():
    sw = client.get("/sw.js").text
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    assert "/owner" not in shell and "/auth" not in shell
    # 껍데기 외 요청은 가로채지 않는다 — respondWith는 /app 화면과 SHELL 정적 자원에만 있다
    assert sw.count("event.respondWith(") == 2
    assert "/owner/" not in sw.split("addEventListener('fetch'")[1].split("addEventListener('push'")[0]
    assert "req.method !== 'GET'" in sw


def test_shell_files_in_sw_all_exist():
    sw = client.get("/sw.js").text
    shell = re.search(r"const SHELL = \[(.*?)\];", sw, re.S).group(1)
    for path in re.findall(r"'(/[^']*)'", shell):
        assert client.get(path).status_code == 200, f"SHELL의 {path}가 서빙되지 않음"


def test_pc_login_redirects_owner_to_mobile_app():
    login = client.get("/login").text
    assert "role === 'owner' ? '/app' : nextAfterLogin()" in login   # 이용자는 항상 모바일로, 관리자는 돌아갈 화면으로


def test_pwa_routes_hidden_from_openapi():
    paths = app.openapi()["paths"]
    for p in ("/app", "/sw.js", "/manifest.webmanifest", "/pwa/{name}"):
        assert p not in paths


def test_manifest_file_is_plain_json():
    from src.api.routers.pwa import PWA_DIR
    json.loads((PWA_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))


def test_app_page_has_signup_and_pending_screens():
    html = client.get("/app").text
    assert "renderSignup" in html and "renderPending" in html
    assert "/signup/options" in html and "'/signup'" in html and "/owner/registration" in html


def test_approvals_page_served_and_linked_from_dashboard():
    r = client.get("/approvals")
    assert r.status_code == 200 and "/registrations" in r.text and "approve" in r.text
    assert 'href="/approvals"' in client.get("/dashboard").text


def test_app_page_has_plan_scope_tabs_only():
    """계획서 범위: 홈(공지·참여 현황)·충전 시간·예측 탭만 있고, 미션·포인트·알림함은 없다."""
    html = client.get("/app").text
    for tab in ("inbox", "price", "forecast"):
        assert f'data-tab="{tab}"' in html
    for gone in ('data-tab="missions"', "/owner/missions", "/owner/points", "/owner/coupons", "/owner/notifications", "/owner/consent/notify"):
        assert gone not in html


def test_signup_and_home_show_supporter_operating_inputs_in_both_languages():
    html = client.get("/app").text
    assert "departure_time" in html and "target_soc_pct" in html and "min_soc_pct" in html and "odometer_km" in html
    assert "su_op_title: '운행 정보(선택)'" in html and "su_op_title: 'Driving info (optional)'" in html


def test_dev_entry_page_links_mobile_pc_admin():
    r = client.get("/dev")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    for href in ('href="/app"', 'href="/dashboard"', 'href="/admin"'):
        assert href in r.text
    assert client.get("/admin").status_code == 200


def test_admin_pages_return_to_themselves_after_login():
    assert "/login?next=/admin" in client.get("/admin").text
    assert "/login?next=/approvals" in client.get("/approvals").text
    login = client.get("/login").text
    assert "nextAfterLogin" in login and "'/admin'" in login and "'/approvals'" in login   # 허용 목록의 내부 경로만


def test_pc_login_hides_quick_login_buttons_the_server_does_not_offer():
    login = client.get("/login").text
    assert "/auth/quick-login/available" in login and "sup.hidden = !avail.super_admin" in login and "adm.hidden = !avail.admin" in login
    assert 'id="quick-owner"' in login and "own.hidden = !avail.owner" in login and "role === 'owner' ? '/app'" in login   # 이용자 빠른 로그인은 모바일로


def test_app_has_my_info_screen_with_rights_and_reapply():
    html = client.get("/app").text
    for s in ("renderMyPage", "/owner/my-data", "/owner/profile", "/owner/password", "/owner/account/withdraw", "/owner/registration",
              "id=\"meBtn\"", "pend_reapply", "mp_withdrawn"):
        assert s in html, s


def test_admin_page_has_ai_ops_kpi_tab():
    html = client.get("/admin").text
    assert 'data-tab="kpi"' in html and "/advisory/ai-ops-kpi" in html and "renderKpi" in html


def test_mobile_guide_shows_recommendation_response_buttons():
    html = client.get("/app").text
    assert "responseCard" in html and "/respond" in html and "rc_yes" in html and "rc_no" in html


def test_owner_session_on_admin_pages_goes_to_login_with_a_reason_not_silently_to_mobile():
    assert "/login?next=/admin&reason=owner" in client.get("/admin").text
    assert "/login?next=/approvals&reason=owner" in client.get("/approvals").text
    login = client.get("/login").text
    assert "reason') === 'owner'" in login and 'id="info"' in login               # 로그인 화면이 이유를 알려 준다
    dev = client.get("/dev").text
    assert 'id="who"' in dev and "/auth/me" in dev and 'id="logout"' in dev        # 진입 화면에 현재 로그인 계정과 로그아웃


def test_every_screen_uses_the_shared_ax_v2g_theme():
    """모든 화면이 /theme.css(AIV2GAPP 기준 서비스 디자인 시스템)를 불러오고, 실측·추론 구분 표시를 쓴다."""
    r = client.get("/theme.css")
    assert r.status_code == 200 and "text/css" in r.headers["content-type"]
    assert "--accent: #1F5EDB" in r.text and "--navy: #0F2A5C" in r.text and 'h2[data-kind="inferred"]::after' in r.text
    for path in ("/login", "/dashboard", "/admin", "/approvals", "/dev", "/app", "/privacy-policy"):
        assert '<link rel="stylesheet" href="/theme.css"' in client.get(path).text, path
    dash = client.get("/dashboard").text
    assert 'class="ax-header"' in dash and 'class="ax-notice"' in dash and 'data-kind="inferred"' in dash
    app_html = client.get("/app").text
    assert '<body class="fo">' in app_html                     # 서포터즈 앱(F.O) 모양
    assert "/owner/ask" not in app_html                         # 삭제된 질문 응답 API를 부르지 않는다


def test_supporter_app_has_aiv2gapp_service_parts():
    """AIV2GAPP 서포터즈 앱에서 옮긴 화면 부품 — 홈 추천 요약, 최소 배터리 바꾸기, 최근 충전 3회, V2G 기여(추론)·적용 전·후, 충전 기록."""
    html = client.get("/app").text
    for key in ("hm_rec:", "ms_sug:", "r3_title:", "vg_title:", "ba_title:", "hs_title:", "function markKind"):
        assert key in html, key
    assert "포인트" not in html and "미션" not in html                     # 리워드(포인트·미션)는 옮기지 않았다



def test_install_page_and_installable_manifest():
    """설치 안내(/install)와 설치 조건 — 매니페스트(이름·시작 주소·standalone·192/512 아이콘), 서비스 워커 등록, 바로가기."""
    r = client.get("/install")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    html = r.text
    assert "beforeinstallprompt" in html and "홈 화면에 추가" in html and "navigator.serviceWorker.register('/sw.js'" in html
    assert '<link rel="manifest" href="/manifest.webmanifest"' in html and '<link rel="stylesheet" href="/theme.css"' in html
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and m["start_url"] == "/app" and m["prefer_related_applications"] is False
    assert {"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]}
    assert [s["url"] for s in m["shortcuts"]] == ["/app?tab=price", "/app?tab=report"]
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js").text
    assert "'/install'" in sw and "skipWaiting" in sw
    app = client.get("/app").text
    assert "controllerchange" in app and "get('tab')" in app and 'href="/install"' in app
