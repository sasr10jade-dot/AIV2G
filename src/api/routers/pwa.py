"""사용자 모바일웹(PWA) 정적 자원 서빙 — 화면(/app), 매니페스트, 서비스 워커, 아이콘.

서비스 워커는 /sw.js(루트 경로)에서 내려줘야 scope가 "/"가 되어 /app 전체를 제어한다.
sw.js와 /app은 항상 최신본을 받도록 no-cache로 보낸다(껍데기 변경이 바로 반영되게).
아이콘은 허용 목록의 파일명만 서빙한다 — 임의 경로 요청(../ 등)은 404.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
PWA_DIR = STATIC_DIR / "pwa"
ICONS = {"icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png"}
IMAGES: set[str] = set()   # 정적 이미지 허용 목록(현재 없음 — 내 차량은 인라인 SVG 일러스트)
NO_CACHE = {"Cache-Control": "no-cache"}


@router.get("/app")
def owner_app() -> FileResponse:
    return FileResponse(STATIC_DIR / "owner.html", headers=NO_CACHE)


@router.get("/privacy-policy")
def privacy_policy() -> FileResponse:
    return FileResponse(STATIC_DIR / "privacy_policy.html", headers=NO_CACHE)


@router.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(PWA_DIR / "manifest.webmanifest", media_type="application/manifest+json", headers=NO_CACHE)


@router.get("/theme.css")
def theme_css() -> FileResponse:
    """모든 화면이 함께 쓰는 AX·V2G 디자인 시스템(색·글꼴·공통 컴포넌트)."""
    return FileResponse(STATIC_DIR / "theme.css", media_type="text/css", headers=NO_CACHE)


@router.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(
        PWA_DIR / "sw.js", media_type="application/javascript",
        headers={**NO_CACHE, "Service-Worker-Allowed": "/"},
    )


@router.get("/pwa/{name}")
def icon(name: str) -> FileResponse:
    if name not in ICONS and name not in IMAGES:
        raise HTTPException(status_code=404)
    return FileResponse(PWA_DIR / name, media_type="image/webp" if name in IMAGES else "image/png", headers={"Cache-Control": "public, max-age=86400"})
