"""Regenerate the marked synthetic demo microscopy and tray photography."""

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

OUT = Path(__file__).resolve().parents[1] / "demo"
W, H = 924, 646
COLORS = [
    (184, 121, 90), (208, 161, 115), (178, 146, 106), (141, 145, 103),
    (100, 135, 108), (102, 124, 134), (101, 119, 111), (136, 114, 105),
    (157, 103, 82), (124, 83, 76), (185, 163, 142), (193, 134, 114),
    (152, 111, 77), (89, 110, 115), (153, 124, 117), (186, 144, 86),
]
FONT = ImageFont.load_default(size=20)
SMALL = ImageFont.load_default(size=14)


def tile(index, drift=0, shape=(142, 114)):
    rng = np.random.default_rng(30421 + index * 55 + drift)
    width, height = shape
    yy, xx = np.mgrid[:height, :width]
    base = np.asarray(COLORS[index % len(COLORS)], dtype=np.float32) + drift * np.array([2, 0.9, -0.8])
    noise = rng.normal(0, 2, (height, width)).astype(np.float32)
    low = ndimage.gaussian_filter(rng.normal(0, 2, (height, width)), 7) * 27
    center = np.abs((xx - width / 2.1) / (width / 2))
    glaze = np.sin((xx + yy * 0.48 + index * 38) / 26) * 8
    curve = (1 - center ** 2) * 8 + 9 * np.cos(yy / height * np.pi * 1.5)
    image = base + (noise + low + glaze + curve)[:, :, None] * np.asarray([1, 0.82, 0.65])
    pixels = Image.fromarray(np.clip(image, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(pixels)
    for _ in range(35):
        x = int(rng.integers(6, width - 6))
        y = int(rng.integers(6, height - 6))
        radius = int(rng.integers(1, 3))
        shade = tuple(np.clip(base + rng.integers(-66, -24), 0, 255).astype(int))
        draw.ellipse((x, y, x + radius * 2, y + radius), fill=shade)
    smooth = Image.new("L", pixels.size)
    ImageDraw.Draw(smooth).rounded_rectangle((1, 1, width - 2, height - 2), radius=14, fill=255)
    pixels.putalpha(smooth)
    return pixels


def tray_file(path, offset, drift, subtitle):
    rng = np.random.default_rng(42 + offset)
    grain = rng.normal(0, 1.8, (H, W, 1))
    base = np.asarray([68, 55, 41]) + grain
    shot = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(shot)
    draw.rectangle((0, 0, W, 75), fill="#30271f")
    draw.text((40, 17), "KILN I  /  WITNESS COUPON TRAY", font=FONT, fill="#f1e1c1")
    draw.text((40, 45), subtitle + "   /   SYNTHETIC DEMONSTRATION", font=SMALL, fill="#c2ab85")
    draw.rounded_rectangle((54, 84, 868, 589), radius=19, fill="#5d4836", outline="#8d765e", width=2)
    start_x, start_y = 69, 107
    stepx, stepy = 196, 151
    for row in range(3):
        for col in range(4):
            left = start_x + col * stepx
            top = start_y + row * stepy
            index = row * 4 + col + offset
            draw.rounded_rectangle((left + 3, top + 2, left + 190, top + 145), radius=9, fill="#4e3929", outline="#796349")
            coupon = tile(index, drift)
            shot.alpha_composite(coupon, (left + 24, top + 8))
            draw.rounded_rectangle((left + 20, top + 119, left + 92, top + 141), radius=3, fill="#eee0c2")
            label = f"K{101 + index}"
            if (row + 2 * col + offset) % 5 == 2:
                label = "   —"
            draw.text((left + 28, top + 122), label, font=SMALL, fill="#332820")
    for index, shade in enumerate(("#35312b", "#797568", "#b4aa96", "#e0d6c2")):
        draw.rectangle((697 + 38 * index, 610, 728 + 38 * index, 633), fill=shade, outline="#a49072")
    shot.save(OUT / path, optimize=True)


def detail():
    width, height = 990, 734
    yy, xx = np.mgrid[:height, :width]
    rng = np.random.default_rng(1514)
    texture = rng.normal(0, 2.0, (height, width))
    mottles = ndimage.gaussian_filter(texture, 8) * 62 + np.sin(xx / 48 + yy / 177) * 11
    bend = (13 * np.cos(yy / 130) + 7 * np.sin(xx / 202) + mottles + texture)
    base = np.array([151, 132, 99]) + bend[..., None] * np.array([1.25, 1, 0.72])
    artwork = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    draw = ImageDraw.Draw(artwork)
    for x in range(7):
        y0 = 90 + 66 * x
        points = [(0, y0), (120, y0 + 22), (270, y0 + 10), (393, y0 + 54),
                  (559, y0 + 69), (761, y0 + 14), (990, y0 - 11)]
        draw.line(points, "#715035", width=3)
    for _, _item in enumerate(range(105)):
        a, b = (int(rng.integers(49, 942)), int(rng.integers(45, 690)))
        radius = int(rng.integers(1, 5))
        draw.ellipse((a, b, a + radius * 2, b + radius), fill="#594235")
    light = Image.new("RGBA", artwork.size)
    lens = ImageDraw.Draw(light)
    lens.ellipse((655, 160, 835, 290), fill="#fff3d4ad")
    lens.ellipse((650, 165, 748, 251), fill="#fff9e9e6")
    artwork.alpha_composite(light)
    alpha = Image.new("L", artwork.size)
    frame = ImageDraw.Draw(alpha)
    frame.rounded_rectangle((38, 34, 956, 696), radius=86, fill=255)
    frame.ellipse((230, 555, 305, 681), fill=0)
    artwork.putalpha(alpha)
    artwork.save(OUT / "amber-detail.png", optimize=True)


def setup():
    for name, index, drift, title in (
        ("east-tray.png", 0, 0, "EAST VIEW  /  TRAY EVA"),
        ("center-tray.png", 2, 3, "CENTER VIEW  /  TRAY EVA"),
        ("west-tray.png", 4, 1, "WEST VIEW  /  TRAY EVA"),
    ):
        tray_file(name, index, drift, title)
    detail()
    options = (("east-tray.png", "01 · 东侧托盘", 0), ("center-tray.png", "02 · 中段托盘", 2), ("west-tray.png", "03 · 西侧托盘", 4))
    allowed = []
    for asset, title, offset in options:
        cells = []
        for row in range(3):
            for col in range(4):
                index = row * 4 + col + offset
                unread = (row + 2 * col + offset) % 5 == 2
                cells.append({"row": row, "column": col, "state": "unread" if unread else "identified",
                              "coupon": "" if unread else f"K{101+index}"})
        allowed.append({"asset": asset, "tray": {
            "name": title[5:], "rows": 3, "columns": 4,
            "origin": [69, 107], "last_column": [853, 107], "last_row": [69, 560], "cells": cells,
        }})
    payload = {"firing": {
        "name": "赤土与烟灰 · 釉色试烧", "kiln": "演示窑 I", "fired_on": "2026-11-03",
        "notes": "演示中的托盘、显微质地和遮挡均为合成图片。点击新窑次可上传自己的照片。",
    }, "photos": [{"asset": asset, "title": title, "role": "overview", "note": "合成演示 · 窑后托盘"}
                 for asset, title, _ in options] + [{"asset": "amber-detail.png", "title": "04 · 琥珀釉面细部",
                                                   "role": "detail", "note": "合成演示 · 带透明实底的开片纹理"}],
       "trays": allowed, "records": [
        {"asset": "east-tray.png", "title": "东盘基准读色", "kind": "color", "settings": {"region": [104, 141, 221, 205]}},
        {"asset": "center-tray.png", "title": "中盘对照读色", "kind": "color", "settings": {"region": [104, 141, 221, 205]}},
        {"asset": "amber-detail.png", "title": "细部标尺", "kind": "ruler",
         "settings": {"start": [100, 100], "end": [600, 100], "millimeters": 35}},
       ]}
    (OUT / "index.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    setup()
