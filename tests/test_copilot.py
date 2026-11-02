"""AI Gemini(EV Bot) 서비스 계층 테스트 — 문서 로딩 회귀 방지 위주.

`DOC_FILES`가 실제 파일 경로와 어긋나면 `_load_docs()`가 조용히 빈 문자열을 반환해
EV Bot이 사업계획 문서 근거 없이 답변하게 된다(예외가 안 나서 눈치채기 어려움) —
이 버그가 한 번 실제로 있었으므로(DOC_FILES가 `Report/` 접두사 없이 프로젝트 루트를
가리켜 전부 미스매치) 문서가 실제로 로드되는지를 직접 검증한다."""
from src.copilot.service import DOC_FILES, _load_docs
from src.paths import PROJECT_ROOT


def test_all_doc_files_exist_on_disk():
    for filename in DOC_FILES:
        assert (PROJECT_ROOT / filename).exists(), f"DOC_FILES 경로가 실제 파일과 어긋남: {filename}"


def test_load_docs_returns_nonempty_content():
    docs = _load_docs()
    assert len(docs) > 1000
    for filename in DOC_FILES:
        assert filename in docs
