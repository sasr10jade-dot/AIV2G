"""AI-V2G 예측·최적화 API — Serving/Decision Layer (기술개발전략 1장 아키텍처 매핑)."""
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from src.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

from .routers import auth, batch, copilot, dashboard, forecast, grid, ingest, models, optimize, privacy, advisory, owner, pwa, signup, registrations, admin_app  # noqa: E402
from src.auth.store import ensure_bootstrap_super_admin  # noqa: E402

TAGS_METADATA = [
    {"name": "auth", "description": "로그인·로그아웃·계정관리 — 최고관리자(super_admin)/관리자(admin)/차량 등록자(owner) 권한."},
    {"name": "owner", "description": "사용자 모바일웹(`/app`) — 차량 등록자(owner)가 본인 차량만 조회·설정. 알림·공지, 충전 시간 안내(안전 검증 포함), 수요 예측, 방전 참여·시범 포인트 적립 동의, 미션·포인트, 상품권 교환·쿠폰함, EV Bot(KR/EN), 내 정보(열람·정정·탈퇴)."},
    {"name": "admin", "description": "웹앱 관리(관리자, `/admin`) — 공지사항(영어 포함), 앱 문구(미션 안내) 편집, 상품권 교환 신청 처리·코드 등록(암호화 저장, 반려 시 포인트 환급)."},
    {"name": "advisory", "description": "추천 계획 발행·수용률(관리자) 및 AI 운영 지표 — 안전 검증 통과율·방전 참여 철회율·EV Bot 호출 집계."},
    {"name": "privacy", "description": "개인정보 보호 — 동의 이력, 해시체인 감사로그와 무결성 검증(최고관리자)."},
    {"name": "health", "description": "서버 상태 확인(공개)."},
    {"name": "signup", "description": "모바일 회원가입 — 계정과 차량·페르소나 정보를 승인 대기로 등록(공개)."},
    {"name": "registrations", "description": "차량 등록 심사(관리자) — 가입 신청을 PC에서 승인·반려하고 차량을 연결."},
    {"name": "ingest", "description": "데이터 연계·적재 (WS1) — 합성 데이터 생성, 실증 데이터 업로드, 파이프라인 실행."},
    {"name": "forecast", "description": "AI 수요·공급 예측 서빙 (WS2) — 적합도 확인, 일/주/월 재귀 예측 전망."},
    {"name": "optimize", "description": "AI 최적 V2G 의사결정 (WS3/WS4) — 일반충전 vs 최적화 PoC 비교."},
    {"name": "models", "description": "예측 모델 레지스트리 조회 (Monitoring 레이어 입력)."},
    {"name": "dashboard", "description": "실시간 웹 대시보드 데이터 (KPI·차트·Closed-loop 통합)."},
    {"name": "copilot", "description": "EV Bot — Gemini 기반 RAG 질의응답 (사업계획 문서 + 시나리오 데이터 근거)."},
    {"name": "grid", "description": "한전 계통 수요반응(DR) 이벤트 — CEMS 외부 신호 수신 스텁, 가격 신호로 V2G 최적화에 반영."},
]

app = FastAPI(
    title="AI-V2G 실증 API",
    version="0.4.0",
    description=(
        "AI기반 도시형 에너지 이익공유 및 전기차 V2G기술 실증 — 데이터 연계 → AI 수요예측 → "
        "V2G 최적화 → 현장 PoC(OCPP) → Closed-loop 재학습까지 PRD 1차년도 워크스트림(WS1~WS5)의 "
        "서빙 계층.\n\n"
        "**화면**: 개발용 진입 `/dev` · 모바일 `/app` · PC 대시보드 `/dashboard` · 어드민 `/admin` · 가입 승인 `/approvals`.\n\n"
        "**권한**: 공개 / 이용자(owner) / 관리자(admin) / 최고관리자(super_admin). 각 엔드포인트의 필요한 권한은 설명 맨 앞 "
        "\"권한\"에 표시된다. 로그인은 `POST /auth/login`(세션 쿠키). 계정은 최고관리자가 `/auth/users`에서 발급하고, 이용자는 "
        "`POST /signup`으로 가입해 관리자 승인 후 차량이 연결된다.\n\n"
        "**보안**: 로그인·비밀번호 확인은 실패가 누적되면 429(Retry-After)로 잠시 잠긴다. 개인정보 응답은 `Cache-Control: no-store`.\n\n"
        "**주의**: 금액·절감 효과·포인트는 합성 데이터와 가정값 기반 시범(모의) 결과이며 실제 금전 정산이 아니다."
    ),
    openapi_tags=TAGS_METADATA,
)

# SESSION_SECRET을 .env에 고정해두지 않으면 서버 재시작마다 무작위 값을 새로 만든다
# (재시작 시 기존 로그인 세션은 전부 풀림 — 데모 규모에서는 재로그인으로 충분).
_session_secret = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
# SESSION_HTTPS_ONLY=1 이면 세션 쿠키에 Secure를 붙이고 HSTS를 보낸다(HTTPS로 공개할 때)
_https_only = os.environ.get("SESSION_HTTPS_ONLY") == "1"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, session_cookie="v2g_session", same_site="lax", https_only=_https_only)

_NO_STORE_PREFIXES = ("/auth", "/owner", "/admin", "/registrations", "/privacy", "/notify", "/signup")


@app.middleware("http")
async def security_headers(request, call_next):
    """모든 응답에 기본 보안 헤더를 붙이고, 개인정보가 오가는 API 응답은 캐시하지 않게 한다."""
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if _https_only:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(_NO_STORE_PREFIXES) and "cache-control" not in h:
        h["Cache-Control"] = "no-store"
    return response

ensure_bootstrap_super_admin()

from .docs_meta import install as _install_docs_meta  # noqa: E402

_install_docs_meta(app, TAGS_METADATA)   # /docs 에 엔드포인트별 필요 권한을 붙인다

app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(batch.router)
app.include_router(privacy.router)
app.include_router(advisory.router)
app.include_router(forecast.router)
app.include_router(optimize.router)
app.include_router(models.router)
app.include_router(dashboard.router)
app.include_router(copilot.router)
app.include_router(grid.router)
app.include_router(owner.router)
app.include_router(pwa.router)
app.include_router(signup.router)
app.include_router(registrations.router)
app.include_router(admin_app.router)

dashboard.start_cache_warmer()
if "pytest" not in __import__("sys").modules:
    from src.advisory import owner_guide as _owner_guide
    _owner_guide.warm_caches_async()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    # 기본은 운영 대시보드. 외부 공개용 데모는 ROOT_REDIRECT=/dev 로 기본 주소가 진입 페이지를 열게 한다(내부 경로만 허용)
    target = os.environ.get("ROOT_REDIRECT", "/dashboard")
    return RedirectResponse(url=target if target.startswith("/") and not target.startswith("//") else "/dashboard")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


@app.get("/dev", include_in_schema=False)
def dev_entry_page() -> FileResponse:
    """개발용 진입 페이지 — 모바일·PC·어드민으로 가는 링크 모음(간편 로그인 버튼은 서버에 개발용 계정이 설정된 경우만)."""
    return FileResponse(STATIC_DIR / "dev.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    """웹앱 관리 화면(공지·앱 문구·상품권 교환) — 화면은 공개, 데이터는 관리자 API가 권한을 확인한다."""
    return FileResponse(STATIC_DIR / "admin.html", headers={"Cache-Control": "no-cache"})


@app.get("/approvals", include_in_schema=False)
def approvals_page() -> FileResponse:
    """가입 승인 화면 — 화면 자체는 공개, 데이터는 관리자 API(/registrations)가 권한을 확인한다."""
    return FileResponse(STATIC_DIR / "approvals.html", headers={"Cache-Control": "no-cache"})


@app.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    return {"status": "ok"}
