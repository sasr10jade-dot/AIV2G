"""API 문서(OpenAPI) 보강 — 각 엔드포인트에 필요한 권한을 코드의 의존성(require_*)에서 읽어 문서에 붙인다.

권한은 사람이 따로 적지 않는다: 라우트가 실제로 걸고 있는 의존성(라우터 수준 포함)을 따라가 가장 높은 단계를 고른다.
그래서 권한을 바꾸면 문서가 함께 바뀐다. `/docs`의 각 항목 설명 맨 앞에 "권한: …"이 붙고, 스키마에는 `x-required-role`이 담긴다.
"""
from __future__ import annotations

from fastapi.routing import APIRoute

# (표시 이름, 단계) — 단계가 높을수록 더 강한 권한. 가장 높은 것을 그 엔드포인트의 권한으로 본다.
_ROLES = {
    "require_super_admin": ("최고관리자", 4),
    "require_admin": ("관리자 이상(관리자·최고관리자)", 3),
    "require_owner": ("이용자(차량이 연결된 차량 등록자)", 2),
    "require_owner_account": ("이용자 계정(승인 전 포함)", 2),
    "require_partner": ("참여기관 연계 토큰(㈜아크릴)", 2),
    "get_current_user": ("로그인 필요", 1),
}
PUBLIC = ("공개(로그인 불필요)", 0)


def _dep_names(dependant) -> list[str]:
    names = []
    if getattr(dependant, "call", None) is not None:
        names.append(getattr(dependant.call, "__name__", ""))
    for sub in getattr(dependant, "dependencies", []) or []:
        names.extend(_dep_names(sub))
    return names


def _flatten(routes):
    """라우트 목록을 (경로·메서드·dependant를 가진 객체)로 펼친다.

    최신 FastAPI는 `include_router`로 붙인 라우터를 `_IncludedRouter`로 지연 보관하므로, 그 안의 실제 라우트는
    `effective_route_contexts()`로 꺼낸다(경로에 접두사가 이미 반영돼 있다). 옛 버전의 중첩 라우터(`.routes`)도 처리한다.
    """
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "effective_route_contexts"):
            yield from r.effective_route_contexts()
        elif hasattr(r, "routes"):
            yield from _flatten(r.routes)


def route_roles(app) -> dict[tuple[str, str], str]:
    """{(메서드, 경로): 필요한 권한 이름}."""
    out: dict[tuple[str, str], str] = {}
    for route in _flatten(app.routes):
        best = PUBLIC
        for name in _dep_names(route.dependant):
            if name in _ROLES and _ROLES[name][1] > best[1]:
                best = _ROLES[name]
        for method in route.methods or []:
            out[(method, route.path)] = best[0]
    return out


def install(app, tags_metadata: list[dict]) -> None:
    """app.openapi 를 교체해 권한 정보를 문서에 붙인다(스키마는 한 번 만들어 캐시한다)."""
    from fastapi.openapi.utils import get_openapi

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes, tags=tags_metadata)
        roles = route_roles(app)
        for path, ops in schema["paths"].items():
            for method, op in ops.items():
                role = roles.get((method.upper(), path))
                if role is None:
                    continue
                op["x-required-role"] = role
                op["description"] = f"**권한:** {role}\n\n" + (op.get("description") or "")
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
