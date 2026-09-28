# -*- coding: utf-8 -*-
"""畫安裝精靈左邊的長條圖和右上角的小圖（要 Pillow）。"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
icon = Image.open(os.path.join(ROOT, "icon-256.png")).convert("RGBA")


def tall(w, h, name):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        d.line([(0, y), (w, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip((0x1a, 0x26, 0x56), (0x25, 0x42, 0xad))))
    s = int(w * 0.62)
    img.paste(icon.resize((s, s), Image.LANCZOS), ((w - s) // 2, int(h * 0.3)), icon.resize((s, s), Image.LANCZOS))
    img.save(os.path.join(ROOT, "installer", name))


def small(size, name):
    img = Image.new("RGB", (size, size), (255, 255, 255))
    img.paste(icon.resize((size, size), Image.LANCZOS), (0, 0), icon.resize((size, size), Image.LANCZOS))
    img.save(os.path.join(ROOT, "installer", name))


tall(164, 314, "wizard.bmp")
tall(328, 628, "wizard_2x.bmp")
small(55, "wizard_small.bmp")
small(110, "wizard_small_2x.bmp")
print("ok")
