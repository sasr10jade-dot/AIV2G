"""사용자 모바일웹(PWA) 아이콘 생성 — 1회성 개발 스크립트(Pillow 필요, 서버 실행에는 불필요).

결과 PNG는 src/api/static/pwa/ 에 저장해 저장소에 포함한다. 디자인을 바꿀 때만 다시 실행한다.
  python scripts/make_pwa_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src" / "api" / "static" / "pwa"
BG = (15, 42, 92)        # AIV2GAPP 네이비(#0F2A5C) — 앱 디자인과 맞춘다
ACCENT = (255, 255, 255)  # 흰 번개(충전·방전)
SS = 4                   # 슈퍼샘플링 배율(가장자리 부드럽게)

# 번개 모양 — 0~100 좌표계, 중앙 정렬
BOLT = [(56, 6), (22, 56), (45, 56), (38, 94), (78, 40), (54, 40), (62, 6)]


def render(size: int, safe: float, rounded: bool) -> Image.Image:
    """safe: 번개가 차지하는 비율(maskable은 안전영역 안에 들어와야 하므로 더 작게)."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if rounded:
        d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 0.22), fill=BG)
    else:
        d.rectangle([0, 0, big, big], fill=BG)  # maskable·apple-touch는 OS가 모서리를 자른다
    scale = big * safe / 100
    off = (big - 100 * scale) / 2
    d.polygon([(off + x * scale, off + y * scale) for x, y in BOLT], fill=ACCENT)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = {
        "icon-192.png": (192, 0.62, True),
        "icon-512.png": (512, 0.62, True),
        "icon-maskable-512.png": (512, 0.46, False),  # 중앙 80% 안전영역 안에 번개가 들어오게
        "apple-touch-icon.png": (180, 0.58, False),
    }
    for name, (size, safe, rounded) in jobs.items():
        render(size, safe, rounded).save(OUT / name, optimize=True)
        print("saved", OUT / name)


if __name__ == "__main__":
    main()
