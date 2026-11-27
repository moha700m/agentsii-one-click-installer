"""Generates the Agentsii logo (PNG) and Windows icon (ICO). Run: python tools/make_assets.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parents[1] / "assets"
S = 1024


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def logo() -> Image.Image:
    c1, c2 = (34, 211, 238), (139, 92, 246)  # cyan -> violet
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        for x in range(S):
            px[x, y] = lerp(c1, c2, (x * 0.45 + y * 0.55) / S)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=230, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)

    # inner glow
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((180, 120, 840, 700), fill=(255, 255, 255, 46))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))

    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)
    # Stylised "A": two strokes + a connector "plug" crossbar
    w = 92
    d.line([(512, 215), (285, 805)], fill=white, width=w, joint="curve")
    d.line([(512, 215), (739, 805)], fill=white, width=w, joint="curve")
    d.ellipse((512 - w // 2, 215 - w // 2, 512 + w // 2, 215 + w // 2), fill=white)
    for x in (285, 739):
        d.ellipse((x - w // 2, 805 - w // 2, x + w // 2, 805 + w // 2), fill=white)
    # crossbar as a link with two nodes
    d.rounded_rectangle((372, 560, 652, 620), radius=30, fill=(255, 255, 255, 235))
    node = (16, 22, 38, 255)
    for cx in (402, 622):
        d.ellipse((cx - 40, 590 - 40, cx + 40, 590 + 40), fill=white)
        d.ellipse((cx - 18, 590 - 18, cx + 18, 590 + 18), fill=node)
    return img


def main():
    OUT.mkdir(exist_ok=True)
    img = logo()
    img.resize((256, 256), Image.LANCZOS).save(OUT / "logo.png")
    img.resize((512, 512), Image.LANCZOS).save(OUT / "logo@2x.png")
    img.save(OUT / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # Inno Setup wizard images (BMP)
    side = Image.new("RGB", (164, 314), (11, 15, 23))
    side.paste(img.resize((120, 120), Image.LANCZOS), (22, 90), img.resize((120, 120), Image.LANCZOS))
    side.save(OUT / "wizard.bmp")
    small = Image.new("RGB", (55, 55), (255, 255, 255))
    small.paste(img.resize((51, 51), Image.LANCZOS), (2, 2), img.resize((51, 51), Image.LANCZOS))
    small.save(OUT / "wizard-small.bmp")
    print("assets written to", OUT)


if __name__ == "__main__":
    main()
