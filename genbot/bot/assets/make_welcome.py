"""Рисует приветственный баннер bot/assets/welcome.png (1280×720).

Нужен только при смене оформления, боту не требуется: pip install pillow && python bot/assets/make_welcome.py
"""
from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

W, H = 1280, 720
HORIZON = 470
BRAND = sys.argv[1] if len(sys.argv) > 1 else "MIRAGE"
TAGLINE = "студия нейросетей · картинки и видео"
TAGS = ("GPT Image", "Nano Banana", "Seedance", "Kling", "Veo")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.png")
FONTS = "/usr/share/fonts/truetype"


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    for path in (f"{FONTS}/dejavu/{name}.ttf", f"{FONTS}/liberation/{name}.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(stops, height):
    """Вертикальный градиент по списку (позиция 0..1, цвет)."""
    img = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
            if p0 <= t <= p1:
                img.putpixel((0, y), lerp(c0, c1, (t - p0) / (p1 - p0)))
                break
    return img.resize((W, height))


def sky() -> Image.Image:
    return gradient([(0, (14, 8, 38)), (0.45, (62, 22, 92)), (0.8, (196, 64, 120)), (1, (255, 150, 110))], HORIZON)


def sun() -> Image.Image:
    """Солнце с прорезями, как в ретро-закате, и мягким свечением."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cx, cy, r = W // 2, HORIZON - 40, 150
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - r - 70, cy - r - 70, cx + r + 70, cy + r + 70), fill=(255, 120, 140, 110))
    layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))
    disc = gradient([(0, (255, 226, 140)), (1, (255, 92, 138))], 2 * r).crop((0, 0, 2 * r, 2 * r))
    mask = Image.new("L", (2 * r, 2 * r), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, 2 * r - 1, 2 * r - 1), fill=255)
    # горизонтальные прорези, к низу толще
    for i in range(7):
        y = int(r * 1.05 + i * i * 2.6 + i * 12)
        md.rectangle((0, y, 2 * r, y + 3 + i * 2), fill=0)
    layer.paste(disc, (cx - r, cy - r), mask)
    return layer


def dunes() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # дюны только над горизонтом, у края кадра выше, в центре почти плоские — солнце видно
    for amp, phase, color in ((46, 0.3, (74, 24, 84, 255)), (30, 2.1, (44, 14, 58, 255))):
        pts = []
        for x in range(0, W + 8, 8):
            edge = min(1.0, abs(x - W / 2) / (W / 2) * 1.6)
            wave = 0.6 * math.sin(x / 210 + phase) + 0.4 * math.sin(x / 97 + phase * 2)
            pts.append((x, HORIZON - amp * edge * (0.7 + 0.3 * wave)))
        d.polygon([(0, HORIZON), *pts, (W, HORIZON)], fill=color)
    return layer


def reflection(scene: Image.Image) -> Image.Image:
    """Мираж: перевёрнутое небо и солнце под горизонтом, с рябью и затуханием."""
    top = scene.crop((0, HORIZON - (H - HORIZON), W, HORIZON)).transpose(Image.FLIP_TOP_BOTTOM)
    rippled = Image.new("RGBA", top.size)
    for y in range(top.height):
        shift = round(6 * math.sin(y / 3.2) * (y / top.height + 0.2))
        row = top.crop((0, y, W, y + 1))
        rippled.paste(ImageChops.offset(row, shift, 0), (0, y))
    fade = Image.linear_gradient("L").resize((W, top.height)).point(lambda v: int((255 - v) * 0.7))
    rippled.putalpha(fade)
    return rippled


def text_layer() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    big = font("DejaVuSans-Bold", 132)
    spacing = 26
    widths = [d.textlength(ch, font=big) for ch in BRAND]
    total = sum(widths) + spacing * (len(BRAND) - 1)
    x, y = (W - total) / 2, 92
    # тень и свечение
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gx = x
    for ch, w in zip(BRAND, widths):
        gd.text((gx, y), ch, font=big, fill=(255, 120, 170, 200))
        gx += w + spacing
    layer.alpha_composite(glow.filter(ImageFilter.GaussianBlur(18)))
    fill = gradient([(0, (255, 244, 230)), (1, (255, 196, 170))], 170)
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    for ch, w in zip(BRAND, widths):
        md.text((x, y), ch, font=big, fill=255)
        x += w + spacing
    colored = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    colored.paste(fill, (0, y - 10))
    layer.paste(colored, (0, 0), mask)

    small = font("DejaVuSans", 30)
    tw = d.textlength(TAGLINE, font=small)
    d.text(((W - tw) / 2, 252), TAGLINE, font=small, fill=(255, 226, 236, 235))
    return layer


def pills() -> Image.Image:
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f = font("DejaVuSans-Bold", 24)
    d = ImageDraw.Draw(layer)
    pad, gap, h = 22, 14, 48
    widths = [d.textlength(t, font=f) + 2 * pad for t in TAGS]
    x, y = (W - sum(widths) - gap * (len(TAGS) - 1)) / 2, H - 92
    for t, w in zip(TAGS, widths):
        pill = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        pd = ImageDraw.Draw(pill)
        pd.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(255, 255, 255, 34),
                             outline=(255, 200, 220, 120), width=2)
        pd.text((x + pad, y + 10), t, font=f, fill=(255, 240, 245, 255))
        layer.alpha_composite(pill)
        x += w + gap
    return layer


def stars(img: Image.Image) -> None:
    import random

    rnd = random.Random(7)
    d = ImageDraw.Draw(img)
    for _ in range(140):
        x, y = rnd.randrange(W), rnd.randrange(HORIZON - 200)
        a = rnd.randrange(60, 220)
        r = rnd.choice((1, 1, 1, 2))
        d.ellipse((x, y, x + r, y + r), fill=(255, 255, 255, a))


def main() -> None:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    img.paste(sky(), (0, 0))
    stars(img)
    img.alpha_composite(sun())
    img.alpha_composite(dunes())
    ground = gradient([(0, (70, 22, 78)), (1, (12, 6, 26))], H - HORIZON)
    img.paste(ground, (0, HORIZON))
    img.alpha_composite(reflection(img), (0, HORIZON))
    # тёплая дымка над горизонтом
    haze = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(haze).rectangle((0, HORIZON - 14, W, HORIZON + 10), fill=(255, 170, 150, 70))
    img.alpha_composite(haze.filter(ImageFilter.GaussianBlur(12)))
    img.alpha_composite(text_layer())
    img.alpha_composite(pills())
    img.convert("RGB").save(OUT, optimize=True)
    print("saved", OUT)


if __name__ == "__main__":
    main()
