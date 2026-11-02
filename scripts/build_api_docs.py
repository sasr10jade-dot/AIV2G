"""OpenAPI에서 API 레퍼런스(Markdown)를 생성한다 — 태그별 표(메서드·경로·필요 권한·설명).

소스가 기준이다: 경로·요약·권한은 코드(라우터·의존성)에서 그대로 뽑으므로 손으로 고치지 않는다.
사용: python scripts/build_api_docs.py [--out docs/api_reference.md]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build() -> str:
    os.environ.setdefault("DISABLE_CACHE_WARMER", "1")
    sys.path.insert(0, str(ROOT))
    from src.api import TAGS_METADATA, app

    spec = app.openapi()
    by_tag: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            tag = (op.get("tags") or ["기타"])[0]
            by_tag[tag].append((method.upper(), path, op.get("x-required-role", ""), op.get("summary", "")))
    desc = {t["name"]: t["description"] for t in TAGS_METADATA}
    total = sum(len(v) for v in by_tag.values())
    order = [t["name"] for t in TAGS_METADATA if t["name"] in by_tag] + sorted(k for k in by_tag if k not in desc)
    lines = [
        "# AI-V2G API 레퍼런스", "",
        f"버전 {spec['info']['version']} · 엔드포인트 {total}개 · 이 문서는 `scripts/build_api_docs.py`가 코드에서 생성한다(직접 고치지 않는다).",
        "대화형 문서는 서버의 `/docs`(Swagger)에서 볼 수 있다.", "",
        "## 권한 단계", "",
        "| 표시 | 의미 |", "|---|---|",
        "| 공개(로그인 불필요) | 누구나 호출 |",
        "| 로그인 필요 | 로그인한 모든 역할 |",
        "| 이용자 계정(승인 전 포함) · 이용자(차량이 연결된 차량 등록자) | 모바일 이용자. 본인 차량만 다룬다 |",
        "| 관리자 이상 | 관리자·최고관리자 |",
        "| 최고관리자 | 계정관리·감사로그 등 |", "",
        "공통: 로그인은 `POST /auth/login`(세션 쿠키). 실패가 누적되면 429(Retry-After). 금액·포인트는 합성 데이터·가정값 기반 시범(모의) 결과다.", "",
    ]
    for tag in order:
        rows = sorted(by_tag[tag], key=lambda r: (r[1], r[0]))
        lines += [f"## {tag}", "", desc.get(tag, ""), "", "| 메서드 | 경로 | 권한 | 설명 |", "|---|---|---|---|"]
        lines += [f"| {m} | `{p}` | {role} | {summary} |" for m, p, role, summary in rows]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "api_reference.md")
    args = ap.parse_args()
    text = build()
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"작성: {args.out} ({text.count(chr(10))}줄)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
