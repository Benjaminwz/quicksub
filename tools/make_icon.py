# -*- coding: utf-8 -*-
"""畫快字幕的圖示：藍色圓角方塊、白色螢幕、兩條字幕、一顆 AI 小星星。
用法：python tools/make_icon.py（要 Pillow）→ 產生 icon.ico、icon-48.png、icon-96.png、icon-256.png"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024  # 先畫大張再縮小，邊緣比較平滑


def star(d, cx, cy, r, fill):
    k = r * 0.28
    pts = [(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k), (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)]
    d.polygon(pts, fill=fill)


img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# 背景：由上往下 寶藍 → 深一點的藍
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
top, bot = (0x44, 0x6a, 0xe6), (0x25, 0x42, 0xad)
for y in range(S):
    t = y / (S - 1)
    gd.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=230, fill=255)
img.paste(grad, (0, 0), mask)

d = ImageDraw.Draw(img)
# 螢幕
d.rounded_rectangle([150, 250, 874, 770], radius=80, fill=(255, 255, 255, 255))
# 畫面裡的播放鍵（淡藍）
d.polygon([(460, 360), (460, 520), (590, 440)], fill=(0xc9, 0xd5, 0xfb, 255))
# 兩條字幕
d.rounded_rectangle([250, 590, 774, 636], radius=23, fill=(0x1a, 0x26, 0x56, 255))
d.rounded_rectangle([340, 664, 684, 706], radius=21, fill=(0x35, 0x58, 0xd4, 255))
# AI 小星星
star(d, 820, 220, 120, (0xff, 0xd8, 0x4d, 255))
star(d, 690, 150, 50, (0xff, 0xe9, 0x9a, 255))

for size in (48, 96, 180, 256):
    img.resize((size, size), Image.LANCZOS).save(os.path.join(ROOT, f"icon-{size}.png"))
img.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "icon.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("ok")
