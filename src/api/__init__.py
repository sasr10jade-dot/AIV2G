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

from .routers import acryl, auth, batch, dashboard, forecast, ingest, models, optimize, privacy, advisory, owner, pwa, signup, registrations, admin_app, cpo  # noqa: E402
from src.auth.store import ensure_bootstrap_super_admin  # noqa: E402

TAGS_METADATA = [
    {"name": "auth", "description": "[01. 인증 및 계정 관리] 세션 로그인·로그아웃, 개발자 빠른 로그인, 계정 발급 및 권한 관리 (최고관리자/관리자/차량등록자)."},
    {"name": "dashboard", "description": "[02. 운영 대시보드 관제] 핵심 성과 요약, 실증 인프라 현황, 차트 데이터, 이익공유 시뮬레이션, 결과 자료(엑셀) 다운로드."},
    {"name": "owner", "description": "[03. 서포터즈 모바일웹 (/app)] 차량 등록자 전용 모바일 앱 — 배터리 모니터링, 충전시간 안내, AI 추천 배너, 거주지 맞춤 추천 충전소, 혼잡 회피 알림함, 충전 리포트."},
    {"name": "signup", "description": "[04. 서포터즈 회원가입] 시·도 / 시·군·구 / 동·읍·면 거주지 선택 및 차량·페르소나 정보 등록 (공개)."},
    {"name": "registrations", "description": "[05. 차량 등록 심사 및 가입 승인] 관리자 승인 화면(/approvals) — 가입 신청 검토, 승인·반려, 실차 EV-ID 매핑, 예비 참여자 관리."},
    {"name": "cpo", "description": "[06. CPO 충전데이터 연계] 충전서비스 사업자(CPO 3사: 나이스인프라·플러그링크·이브이시스 150기) 데이터 연계 명세 및 일 단위 세션 수신 REST API."},
    {"name": "admin", "description": "[07. 웹앱 관리 (/admin)] 서포터즈 앱 공지사항 CRUD, 다국어 앱 문구, AI 모델링 예외상황 기록 관리."},
    {"name": "models", "description": "[08. AI 예측모델 레지스트리] AI 기반 최적 V2G 예측모델 설명서(Model Card 공식 산출물) 및 모델 메타데이터 조회."},
    {"name": "forecast", "description": "[09. AI 충전수요 전망] 48시간 일별/주별/월별 미래 충전수요 시계열 재귀 예측 서빙 (㈜아크릴 예측 연계)."},
    {"name": "optimize", "description": "[10. AI 최적 V2G 의사결정] 일반 충전(무제어) vs AI-V2G 최적화 피크 저감 및 충전요금 절감 효과 비교 (HiGHS LP)."},
    {"name": "acryl", "description": "[11. ㈜아크릴 AI 예측모델 연계] 참여기관 ㈜아크릴 간 학습용 정제 데이터 제공(가명화) 및 예측 추론 결과 배치 수신."},
    {"name": "privacy", "description": "[12. 개인정보 보호 및 감사로그] 개인정보 가명화(HMAC-SHA256), 동의 이력 관리, 감사로그 해시체인 무결성 검증."},
    {"name": "ingest", "description": "[13. 데이터 수급 파이프라인] 서포터즈 실차 OBD2 배치 수급 및 전력거래소(KPX)·기상청 공공데이터 동기화."},
    {"name": "advisory", "description": "[14. AI 운영 지표] AI 추천 발행, 차주 수용률 및 안전 검증 통과율 감사로그 집계."},
    {"name": "health", "description": "[15. 서버 헬스체크] 시스템 및 데이터베이스 가동 상태 확인 (공개)."},
]

app = FastAPI(
    title="AI-V2G 실증 시스템 공식 API 레퍼런스",
    version="1.0.0",
    description=(
        "**과제명**: 올인원 AI 허브 AX 실증 (에너지·모빌리티) — AI 기반 도시형 에너지 이익공유 및 전기차 V2G 기술 실증\n\n"
        "실증 시스템에서 실제로 운영되는 **서포터즈 실차 30대 OBD2 수급**, **CPO 3사 150기 연계**, **공공데이터 5종 동기화**, "
        "**AI 충전수요 예측(㈜아크릴)** 및 **V2G 의사결정(HiGHS LP)** 서빙 계층의 실무 API 목록입니다.\n\n"
        "### 주요 화면 링크\n"
        "* **개발자 진입점**: [`/dev`](/dev) (최고관리자 원클릭 로그인 지원)\n"
        "* **운영 대시보드**: [`/dashboard`](/dashboard) (LNB 사이드바, CPO 150기 관제 리스트, 차트 188px 통일)\n"
        "* **모바일 서포터즈 앱**: [`/app`](/app) (시군구동 가입, 거주지 맞춤 추천 충전소, 혼잡 회피 알림)\n"
        "* **가입 승인 관리**: [`/approvals`](/approvals) (서포터즈 신청 검토 및 실차 매핑)\n"
        "* **웹앱 관리**: [`/admin`](/admin) (모바일 공지사항, 앱 문구, 모델링 예외 기록)\n\n"
        "### 권한 체계 (RBAC)\n"
        "* **공개 (Public)**: 로그인 불필요 (가입, 로그인, 헬스체크)\n"
        "* **이용자 (Owner)**: 차량이 연결된 실증 서포터즈 차주 (본인 차량 데이터만 열람·설정)\n"
        "* **관리자 (Admin)**: 가입 승인 심사, 공지사항 등록, 모델링 예외 처리\n"
        "* **최고관리자 (Super Admin)**: 계정 발급/삭제, 감사로그 해시체인 무결성 검증, 데이터 수급 관리"
    ),
    openapi_tags=TAGS_METADATA,
)

# SESSION_SECRET을 .env에 고정해두지 않으면 서버 재시작마다 무작위 값을 새로 만든다
# (재시작 시 기존 로그인 세션은 전부 풀림 — 데모 규모에서는 재로그인으로 충분).
_session_secret = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
# SESSION_HTTPS_ONLY=1 이면 세션 쿠키에 Secure를 붙이고 HSTS를 보낸다(HTTPS로 공개할 때)
_https_only = os.environ.get("SESSION_HTTPS_ONLY") == "1"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, session_cookie="v2g_session", same_site="lax", https_only=_https_only)

_NO_STORE_PREFIXES = ("/auth", "/owner", "/admin", "/registrations", "/privacy", "/signup", "/acryl", "/partner")


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
app.include_router(owner.router)
app.include_router(pwa.router)
app.include_router(signup.router)
app.include_router(registrations.router)
app.include_router(admin_app.router)
app.include_router(acryl.router)
app.include_router(cpo.router, prefix="/cpo", tags=["cpo"])

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
