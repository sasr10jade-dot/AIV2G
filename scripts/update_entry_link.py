"""고정 진입 링크(GitHub Pages `AIV2G`)가 가리키는 서비스 주소를 바꾼다.

고정 주소: https://sasr10jade-dot.github.io/AIV2G/  → 이 페이지가 현재 서비스(터널) 주소로 자동 이동한다.
터널을 다시 띄워 서비스 주소가 바뀌면 이 스크립트로 목적지만 갱신하면 된다(소개서에 넣은 고정 주소는 그대로).

사용:
  python scripts/update_entry_link.py https://새로운-주소.trycloudflare.com          # 페이지 파일만 고쳐 보여 준다(dry-run)
  python scripts/update_entry_link.py https://새로운-주소.trycloudflare.com --push   # 커밋하고 푸시한다

작업 폴더(기본: 저장소 옆 `aiv2g-entry`)가 없으면 `gh repo clone`으로 받는다. 서비스 주소는 https:// 로 시작해야 한다.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "sasr10jade-dot/AIV2G"
PAGES_URL = "https://sasr10jade-dot.github.io/AIV2G/"


def run(cmd: list[str], cwd: Path, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise SystemExit(f"명령 실패: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="새 서비스 주소(예: https://xxxx.trycloudflare.com)")
    ap.add_argument("--target", type=Path, default=ROOT.parent / "aiv2g-entry")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--push", action="store_true")
    args = ap.parse_args()

    url = args.url.strip().rstrip("/") + "/"
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?/", url):
        raise SystemExit("주소는 https://호스트 형식이어야 합니다(경로·공백 없음).")
    target: Path = args.target
    if not (target / ".git").exists():
        run(["gh", "repo", "clone", args.repo, str(target)], cwd=ROOT.parent)
    else:
        run(["git", "pull", "--ff-only", "-q"], cwd=target, check=False)

    page = target / "index.html"
    html = page.read_text(encoding="utf-8")
    new_html, n1 = re.subn(r'var TARGET = "[^"]*";', f'var TARGET = "{url}";', html)
    new_html, n2 = re.subn(r'content="0; url=[^"]*"', f'content="0; url={url}"', new_html)
    if n1 != 1 or n2 != 1:
        raise SystemExit("index.html 형식이 예상과 달라 바꾸지 않았습니다.")
    if new_html == html:
        print(f"이미 같은 주소입니다: {url}")
        return 0
    page.write_text(new_html, encoding="utf-8", newline="")
    print(f"목적지 변경: {url}\n고정 주소: {PAGES_URL}")
    if not args.push:
        print("(dry-run) 파일만 고쳤고 커밋·푸시는 하지 않았습니다. 반영하려면 --push 로 다시 실행하세요.")
        return 0
    name = run(["git", "config", "user.name"], cwd=ROOT, check=False).strip() or "AI-V2G"
    email = run(["git", "config", "user.email"], cwd=ROOT, check=False).strip() or "noreply@example.com"
    run(["git", "add", "index.html"], cwd=target)
    run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", f"서비스 주소 변경: {url}"], cwd=target)
    run(["git", "push", "-q"], cwd=target)
    print("푸시 완료. 반영까지 1분 안팎 걸릴 수 있습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
