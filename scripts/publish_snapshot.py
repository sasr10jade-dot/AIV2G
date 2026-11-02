"""공개(비공개 원격) 저장소로 코드 스냅샷을 동기화한다.

원본 저장소에는 사업 문서(`Report/`)와 회의용 PPT가 커밋돼 있어, 이력째 올리지 않고 **커밋된 최신 상태에서 제외 경로를 뺀
스냅샷**을 별도 작업 폴더에 반영해 새 커밋으로 올린다(이력은 동기화 단위로만 쌓인다).

사용(저장소 루트에서, 또는 `python ../scripts/publish_snapshot.py`처럼 경로를 맞춰서):
  python scripts/publish_snapshot.py                 # 무엇이 바뀌는지만 보여 준다(dry-run, 기본)
  python scripts/publish_snapshot.py --push          # 커밋하고 GitHub로 푸시한다
  python scripts/publish_snapshot.py --target 폴더    # 작업 폴더 위치 지정(기본: 저장소 옆 ai-v2g-publish)
  python scripts/publish_snapshot.py --repo 계정/이름  # 원격 저장소 지정(기본: 아래 DEFAULT_REPO)

원본에서 **커밋된** 내용만 반영한다(작업 중인 미커밋 변경은 올라가지 않는다). 푸시는 `gh`에 로그인돼 있어야 한다.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

DEFAULT_REPO = "sasr10jade-dot/sasr10jade-dot_AIV2G"
# 올리지 않는 경로(앞부분 일치 또는 glob) — 사업 문서·회의 자료·개인정보가 섞일 수 있는 파일
EXCLUDE = ["Report/", "docs/meeting_*.pptx", "*.hwp", "*.hwpx", "*.xlsx", "*.docx", "*.pptx", "data/", ".env"]

ROOT = Path(__file__).resolve().parents[1]
NUL = chr(0)   # git ls-files -z 의 구분자(소스에 널 문자를 직접 쓰지 않으려고 chr로 만든다)


def run(cmd: list[str], cwd: Path, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise SystemExit(f"명령 실패: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout


def excluded(path: str) -> bool:
    return any(path.startswith(p) if p.endswith("/") else fnmatch.fnmatch(path, p) or fnmatch.fnmatch(Path(path).name, p) for p in EXCLUDE)


def export_tree() -> dict[str, bytes]:
    """커밋된 HEAD를 읽어 제외 경로를 뺀 {경로: 내용}을 만든다."""
    data = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True, check=True).stdout
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for m in tar.getmembers():
            if m.isfile() and not excluded(m.name):
                files[m.name] = tar.extractfile(m).read()
    return files


def main() -> int:
    # 윈도 콘솔(cp949)에서 출력할 수 없는 문자가 있어도 죽지 않게 한다
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", type=Path, default=ROOT.parent / "ai-v2g-publish")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--push", action="store_true", help="커밋하고 푸시한다(없으면 변경 요약만 출력)")
    args = ap.parse_args()
    target: Path = args.target

    if not (target / ".git").exists():
        if not args.push:
            print(f"[안내] 작업 폴더가 없습니다: {target}\n  --push 로 실행하면 `gh repo clone {args.repo}` 후 반영합니다.")
            return 0
        run(["gh", "repo", "clone", args.repo, str(target)], cwd=ROOT.parent)
    else:
        # 작업 폴더는 이 스크립트만 쓰는 일회용 사본이다 — 이전 실행이 중간에 실패해 남은 변경·커밋이 있어도
        # 원격 최신 상태로 되돌린 뒤 시작한다(그래야 push가 거절되지 않는다).
        run(["git", "fetch", "-q", "origin"], cwd=target)
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=target).strip()
        run(["git", "reset", "--hard", "-q", f"origin/{branch}"], cwd=target)
        run(["git", "clean", "-fdq"], cwd=target)

    new = export_tree()
    # 한글 경로가 따옴표·8진수로 나오지 않게 quotepath를 끄고 널 구분(-z)으로 읽는다
    old = {p for p in run(["git", "-c", "core.quotepath=false", "ls-files", "-z"], cwd=target).split(NUL) if p}
    removed = sorted(old - set(new))
    for rel, content in new.items():
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists() or dest.read_bytes() != content:
            dest.write_bytes(content)
    for rel in removed:
        (target / rel).unlink(missing_ok=True)
    for d in sorted((p for p in target.rglob("*") if p.is_dir() and ".git" not in p.parts), reverse=True):
        if not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)

    run(["git", "add", "-A"], cwd=target)
    stat = run(["git", "-c", "core.quotepath=false", "diff", "--cached", "--stat"], cwd=target).strip()
    if not stat:
        print("변경 없음: 이미 최신입니다.")
        return 0
    print(stat)
    if not args.push:
        run(["git", "reset", "-q"], cwd=target)
        print("\n(dry-run) 작업 폴더에 파일만 반영했고 커밋·푸시는 하지 않았습니다. 올리려면 --push 로 다시 실행하세요.")
        return 0
    sha = run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT).strip()
    subject = run(["git", "log", "-1", "--format=%s"], cwd=ROOT).strip()
    msg = f"sync: {sha} {subject}\n\n사업 문서·회의 자료를 제외한 코드 스냅샷 동기화."
    # 작업 폴더에는 git 사용자 설정이 없을 수 있어, 원본 저장소의 이름·이메일을 그대로 쓴다
    name = run(["git", "config", "user.name"], cwd=ROOT, check=False).strip() or "AI-V2G"
    email = run(["git", "config", "user.email"], cwd=ROOT, check=False).strip() or "noreply@example.com"
    run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", msg], cwd=target)
    run(["git", "push", "-q"], cwd=target)
    print(f"푸시 완료: {args.repo} ({sha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
