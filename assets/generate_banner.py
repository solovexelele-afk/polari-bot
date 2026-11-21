"""Генерирует баннер POLARI (assets/banners/polari_banner.png). Запуск: python assets/generate_banner.py"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1200, 400
OUT = Path(__file__).resolve().parent / "banners" / "polari_banner.png"
TOP, BOTTOM = (6, 12, 28), (15, 42, 92)          # тёмный фон -> тёмно-синий
GLOW, ACCENT = (46, 134, 222), (127, 219, 255)    # голубой и светло-голубой


def font(size: int) -> ImageFont.ImageFont:
    for name in ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def main() -> None:
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):  # вертикальный градиент
        t = y / H
        draw.line([(0, y), (W, y)], fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)))

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # северное сияние
    g = ImageDraw.Draw(glow)
    for k in range(3):
        pts = [(x, 150 + 60 * math.sin(x / 140 + k) + k * 40) for x in range(0, W + 20, 20)]
        g.line(pts, fill=(*GLOW, 90 - k * 20), width=36 - k * 8)
    img.paste(glow.filter(ImageFilter.GaussianBlur(28)), (0, 0), glow.filter(ImageFilter.GaussianBlur(28)))

    rnd = random.Random(7)  # звёзды
    for _ in range(140):
        x, y, r = rnd.randint(0, W), rnd.randint(0, H), rnd.choice([1, 1, 1, 2])
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(200, 230, 255))

    title = font(150)
    text = "POLARI"
    box = draw.textbbox((0, 0), text, font=title)
    x, y = (W - (box[2] - box[0])) // 2, (H - (box[3] - box[1])) // 2 - 40
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y), text, font=title, fill=(*GLOW, 255))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    img.paste(shadow, (0, 0), shadow)
    draw.text((x, y), text, font=title, fill=(235, 246, 255))

    sub = font(30)
    tagline = "❄  сервер с характером  ❄".replace("❄", "—")
    sb = draw.textbbox((0, 0), tagline, font=sub)
    draw.text(((W - (sb[2] - sb[0])) // 2, y + (box[3] - box[1]) + 70), tagline, font=sub, fill=ACCENT)
    draw.line([(W // 2 - 260, H - 40), (W // 2 + 260, H - 40)], fill=ACCENT, width=2)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, optimize=True)
    print(f"Баннер сохранён: {OUT}")


if __name__ == "__main__":
    main()
