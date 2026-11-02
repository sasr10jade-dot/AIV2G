"""API 문서 — 모든 엔드포인트에 필요한 권한이 표시되고, 생성 문서(docs/api_reference.md)가 코드와 어긋나지 않는다."""
import importlib.util
from pathlib import Path

from src.api import TAGS_METADATA, app

ROOT = Path(__file__).resolve().parents[1]


def _ops():
    for path, ops in app.openapi()["paths"].items():
        for method, op in ops.items():
            yield method.upper(), path, op


def test_every_operation_shows_required_role_and_declared_tag():
    declared = {t["name"] for t in TAGS_METADATA}
    for method, path, op in _ops():
        assert op.get("x-required-role"), f"권한 표시 없음: {method} {path}"
        assert op["description"].startswith("**권한:**"), f"설명에 권한이 없음: {method} {path}"
        assert op["tags"][0] in declared, f"선언되지 않은 태그 {op['tags']}: {method} {path}"
        assert op.get("summary"), f"요약 없음: {method} {path}"


def test_roles_match_real_dependencies():
    roles = {(m, p): op["x-required-role"] for m, p, op in _ops()}
    assert roles[("POST", "/signup")].startswith("공개")
    assert roles[("POST", "/auth/login")].startswith("공개")
    assert roles[("GET", "/auth/users")] == "최고관리자"
    assert roles[("GET", "/privacy/audit")] == "최고관리자"
    assert roles[("GET", "/admin/notices")].startswith("관리자")
    assert roles[("GET", "/registrations")].startswith("관리자")
    assert roles[("GET", "/owner/charging-guide")].startswith("이용자")
    assert roles[("GET", "/owner/registration")].startswith("이용자 계정")


def test_generated_api_reference_is_up_to_date():
    spec = importlib.util.spec_from_file_location("build_api_docs", ROOT / "scripts" / "build_api_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    current = (ROOT / "docs" / "api_reference.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert current.rstrip("\n") == mod.build().rstrip("\n"), "docs/api_reference.md가 오래됨 — python scripts/build_api_docs.py 로 다시 생성하세요"
