"""EV Bot 참고 문서 생성 — 사업수행계획서(pptx)에서 질의응답에 쓸 슬라이드를 원문 그대로 발췌한다.

요약을 새로 쓰면 사실이 어긋날 수 있어, 선택한 슬라이드의 텍스트를 가공 없이 옮긴다(표는 셀 단위로 펼쳐져
줄이 많다). 계획서가 바뀌면 다시 실행해 docs/evbot/ 를 갱신한다.

  python scripts/build_evbot_docs.py
  python scripts/build_evbot_docs.py --pptx "다른 계획서.pptx"

예산·인력·제출서류 슬라이드는 EV Bot이 답할 일이 없고 프롬프트 길이만 늘리므로 뺀다.
"""
import argparse
import html
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PPTX = ROOT / "Report" / "AI-V2G_사업수행계획서(개발POC 기능 적용)_v1.0_261021.pptx"
OUT = ROOT / "docs" / "evbot" / "사업수행계획서_발췌_v1.0.md"

# 요약서·사업 개요·목표·1차년도 성과지표와 추진계획·B2C 서비스·리스크·기대효과
SLIDES = [2, 3, 5, 6, 7, 8, 12, 13, 15, 16, 17, 18, 19, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 32, 34, 35, 39, 47, 48]


def slide_text(zf: zipfile.ZipFile, n: int) -> str:
    xml = zf.read(f"ppt/slides/slide{n}.xml").decode("utf-8")
    paras = []
    for p in re.findall(r"<a:p>.*?</a:p>", xml, re.S):
        text = html.unescape("".join(re.findall(r"<a:t>([^<]*)</a:t>", p))).strip()
        if text:
            paras.append(text)
    return "\n".join(paras)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pptx", type=Path, default=DEFAULT_PPTX)
    args = ap.parse_args()
    if not args.pptx.exists():
        print(f"계획서를 찾을 수 없습니다: {args.pptx}", file=sys.stderr)
        return 1
    with zipfile.ZipFile(args.pptx) as zf:
        sections = [f"## 슬라이드 {n}\n{slide_text(zf, n)}" for n in SLIDES]
    header = (
        "# AI-V2G 사업수행계획서 발췌 (EV Bot 참고 문서)\n\n"
        f"출처: {args.pptx.name} — 선택한 슬라이드 {len(SLIDES)}장의 원문 텍스트. 표는 셀 단위로 펼쳐져 있다.\n"
        "이 문서에 없는 내용(예: KPI 가중치, 예산 세부)은 답하지 말고 '문서에 없다'고 답한다.\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(header + "\n" + "\n\n".join(sections) + "\n", encoding="utf-8")
    print(f"{OUT} — {OUT.stat().st_size:,} bytes, {len(SLIDES)} slides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
