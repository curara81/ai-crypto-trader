#!/usr/bin/env python3
"""PWA 아이콘 생성 — PIL 사용.

다크 그라데이션 배경 + 가운데 "AI" 텍스트.
192, 512, 180(apple-touch-icon) 3개 생성.
"""
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("PIL 미설치, 단순 단색 PNG 생성")
    Image = None

STATIC = Path(__file__).parent / "static"
STATIC.mkdir(parents=True, exist_ok=True)


def make_icon(size: int, output: Path):
    if Image is None:
        # PIL 없으면 minimum valid PNG (1x1)
        # https://garethrees.org/2007/11/14/pngcrush/
        png_1x1 = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d49444154789c63f8cffe1f000005fe027fdc6f0bc20000000049454e44ae426082"
        )
        output.write_bytes(png_1x1)
        return

    # 다크 → 진녹색 그라데이션
    img = Image.new("RGB", (size, size), (10, 14, 20))
    draw = ImageDraw.Draw(img)

    # 라운드 사각형 (배경)
    radius = int(size * 0.22)
    # 그라데이션 효과를 단순 색상 변화로 구현
    for y in range(size):
        ratio = y / size
        r = int(10 + ratio * 18)    # 10 → 28
        g = int(20 + ratio * 38)    # 20 → 58
        b = int(30 + ratio * 50)    # 30 → 80
        draw.line([(0, y), (size, y)], fill=(r, g, b))

    # "AI" 텍스트
    try:
        font_size = int(size * 0.42)
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except Exception:
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None

    if font:
        text = "AI"
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x = (size - tw) // 2
        y = (size - th) // 2 - int(size * 0.05)
        # 그림자
        draw.text((x + 2, y + 2), text, fill=(0, 0, 0, 100), font=font)
        # 본문 (네온 그린)
        draw.text((x, y), text, fill=(74, 222, 128), font=font)

    # 작은 점 (액센트)
    dot_r = max(int(size * 0.04), 2)
    draw.ellipse(
        [size - int(size * 0.18) - dot_r, int(size * 0.18) - dot_r,
         size - int(size * 0.18) + dot_r, int(size * 0.18) + dot_r],
        fill=(248, 113, 113)
    )

    img.save(str(output), "PNG", optimize=True)


def main():
    sizes = {
        "icon-192.png": 192,
        "icon-512.png": 512,
        "apple-touch-icon.png": 180,
    }
    for name, size in sizes.items():
        path = STATIC / name
        make_icon(size, path)
        print(f"  ✓ {name} ({size}x{size})")


if __name__ == "__main__":
    main()
