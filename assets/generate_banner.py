"""Генерирует баннеры POLARI (assets/banners/*.png). Запуск: python assets/generate_banner.py

Стиль: тёмный фон, размытые голубые лепестки, плашка «DISCORD SERVER | POLARI»,
крупный белый заголовок со свечением и голубой подзаголовок.
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1800, 720
OUT = Path(__file__).resolve().parent / "banners"
BLUE, LIGHT = (59, 130, 246), (125, 211, 252)

BANNERS = {
    "polari_banner": ("POLARI", "сервер с характером"),
    "memo": ("ПАМЯТКА", "по серверу"),
    "rules": ("ПРАВИЛА", "сервера"),
    "shop": ("МАГАЗИН", "сервера"),
    "tickets": ("ПОДДЕРЖКА", "сервера"),
    "info": ("ДОСТУП", "к серверу"),
    "welcome": ("ПРИВЕТ", "добро пожаловать"),
    "staff_memo": ("ПАМЯТКА", "для персонала"),
    "interview": ("ОБЗВОН", "панель проверки"),
    "news": ("НОВОСТИ", "сервера"),
    "music": ("МУЗЫКА", "радио сервера"),
}


def font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    names = ["seguibl.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["segoeui.ttf", "arial.ttf",
                                                                                   "DejaVuSans.ttf"]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def petal(size: int, rnd: random.Random) -> Image.Image:
    """Один лепесток: эллипс с градиентом от голубого к синему и прожилкой."""
    canvas = int(size * 1.6)  # запас, чтобы размытие и поворот не обрезали края
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c0 = canvas / 2
    for i in range(size // 2, 0, -2):
        t = i / (size / 2)
        c = tuple(int(LIGHT[k] * (1 - t) + BLUE[k] * t) for k in range(3))
        # лепесток: заострённый к одному краю эллипс
        d.ellipse([c0 - i, c0 - i * 0.58, c0 + i * 0.85, c0 + i * 0.58], fill=(*c, 230))
    return img.rotate(rnd.randint(0, 360), resample=Image.BICUBIC)


def make(key: str, title: str, subtitle: str) -> None:
    rnd = random.Random(hash(key) % 1000)
    img = Image.new("RGB", (W, H), (12, 13, 16))
    # мягкое пятно света по центру
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([W * 0.25, H * 0.1, W * 0.85, H * 1.1], fill=(30, 40, 60, 140))
    img.paste(glow.filter(ImageFilter.GaussianBlur(160)), (0, 0), glow.filter(ImageFilter.GaussianBlur(160)))

    # лепестки по углам: дальние сильно размыты, ближние — чётче
    spots = [(-80, -120, 520), (380, -200, 420), (-120, 470, 560), (330, 560, 460), (1500, -150, 480),
             (1480, 480, 560), (1180, 620, 380), (920, -180, 300)]
    for x, y, s in spots:
        p = petal(s, rnd).filter(ImageFilter.GaussianBlur(rnd.choice([10, 18, 28])))
        off = int(s * 0.3)  # центрируем увеличенный холст
        img.paste(p, (x - off, y - off), p)

    # шум как на референсе
    noise = Image.effect_noise((W, H), 18).convert("L")
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.05)
    draw = ImageDraw.Draw(img)

    # плашка DISCORD SERVER | POLARI
    pill_font = font(30, bold=False)
    px, py = 150, 240
    left, right = "DISCORD SERVER", "POLARI"
    lw = draw.textlength(left, font=pill_font)
    rw = draw.textlength(right, font=pill_font)
    draw.rounded_rectangle([px, py, px + lw + rw + 110, py + 70], radius=35, outline=(70, 75, 90), width=2)
    draw.text((px + 35, py + 17), left, font=pill_font, fill=(235, 238, 245))
    sx = px + 35 + lw + 20
    draw.line([(sx, py + 12), (sx, py + 58)], fill=BLUE, width=3)
    draw.text((sx + 20, py + 17), right, font=pill_font, fill=LIGHT)

    # заголовок со свечением
    size = 190 if len(title) <= 8 else int(190 * 8 / len(title))
    tf = font(size)
    ty = 330
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((px - 5, ty), title, font=tf, fill=(170, 200, 255, 200))
    shadow = shadow.filter(ImageFilter.GaussianBlur(22))
    img.paste(shadow, (0, 0), shadow)
    draw = ImageDraw.Draw(img)
    draw.text((px - 5, ty), title, font=tf, fill=(255, 255, 255))
    tb = draw.textbbox((px - 5, ty), title, font=tf)

    sf = font(72, bold=False)
    sw = draw.textlength(subtitle, font=sf)
    sub_glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sub_glow).text((tb[2] - sw, tb[3] + 5), subtitle, font=sf, fill=(*BLUE, 220))
    img.paste(sub_glow.filter(ImageFilter.GaussianBlur(10)), (0, 0), sub_glow.filter(ImageFilter.GaussianBlur(10)))
    draw = ImageDraw.Draw(img)
    draw.text((tb[2] - sw, tb[3] + 5), subtitle, font=sf, fill=(96, 165, 250))

    # снежинка-эмблема справа
    cx, cy, r = 1420, 360, 170
    emb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    e = ImageDraw.Draw(emb)
    for k in range(6):
        a = math.pi / 3 * k
        x2, y2 = cx + r * math.cos(a), cy + r * math.sin(a)
        e.line([(cx, cy), (x2, y2)], fill=(*LIGHT, 255), width=10)
        for f in (0.55, 0.8):
            bx, by = cx + r * f * math.cos(a), cy + r * f * math.sin(a)
            for s in (-1, 1):
                b = a + s * math.pi / 4
                e.line([(bx, by), (bx + 45 * math.cos(b), by + 45 * math.sin(b))], fill=(*LIGHT, 255), width=8)
    e.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], fill=(255, 255, 255, 255))
    img.paste(emb.filter(ImageFilter.GaussianBlur(25)), (0, 0), emb.filter(ImageFilter.GaussianBlur(25)))
    img.paste(emb, (0, 0), emb)

    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / f"{key}.png", optimize=True)


def main() -> None:
    for key, (title, subtitle) in BANNERS.items():
        make(key, title, subtitle)
        print("готово:", key)


if __name__ == "__main__":
    main()
