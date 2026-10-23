"""Рисует иконки сайта (главный экран iPhone, PWA) в app/static/icons.

    python tools/make_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
TOP = (66, 98, 232)
BOTTOM = (40, 62, 178)
BARS = [0.34, 0.62, 0.92, 0.55, 0.78, 0.40]  # высота столбиков «звуковой волны»


def draw(size: int, rounded: bool) -> Image.Image:
    scale = 4  # рисуем крупнее и уменьшаем — края получаются гладкими
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    grad = Image.new("RGBA", (1, s))
    for y in range(s):
        t = y / (s - 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    grad = grad.resize((s, s))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.225) if rounded else 0, fill=255)
    img.paste(grad, (0, 0), mask)

    d = ImageDraw.Draw(img)
    area = s * 0.56  # вся волна — в безопасной зоне маскируемой иконки
    bar_w = area / (len(BARS) * 1.75)
    gap = (area - bar_w * len(BARS)) / (len(BARS) - 1)
    x = (s - area) / 2
    for h in BARS:
        bh = area * h
        y0 = (s - bh) / 2
        d.rounded_rectangle([x, y0, x + bar_w, y0 + bh], radius=bar_w / 2, fill=(255, 255, 255, 255))
        x += bar_w + gap
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192, True).save(OUT / "icon-192.png")
    draw(512, True).save(OUT / "icon-512.png")
    draw(512, False).save(OUT / "maskable-512.png")
    # iOS сам скругляет углы и не любит прозрачность
    draw(180, False).convert("RGB").save(OUT / "apple-touch-icon.png")
    print(f"Готово: {OUT}")


if __name__ == "__main__":
    main()
