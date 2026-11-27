"""Produce tracing crops, sampling strips and batch color consistency maps."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw

from .calibration import analyze, cielab, delta_e, encode, linearize, weighted_means
from .geometry import Box, Region, evenly_spaced
from .imaging import crop_region, flatten, image_samples, rotated_crop, window
from .inputs import InputError, coordinate, flag, integer, number, obj, seq


@dataclass(frozen=True)
class Path:
    vertices: tuple
    total: float

    @classmethod
    def read(cls, value, width, height):
        vertices = []
        for entry in seq(value, "折线节点", minimum=2, maximum=24):
            x, y = coordinate(entry, "折线节点")
            if not 0 <= x <= width or not 0 <= y <= height:
                raise InputError("折线节点超出照片")
            vertices.append((x, y))
        lengths = [math.dist(a, b) for a, b in zip(vertices, vertices[1:])]
        if min(lengths) < 1:
            raise InputError("相邻节点至少相隔一个像素")
        return cls(tuple(vertices), math.fsum(lengths))

    def evenly_sample(self, spacing):
        if self.total / spacing > 1900:
            raise InputError("折线采样超过 1900 点，请增加采样间距")
        xx, yy, travel, segment = [], [], [], []
        offset = 0
        for index, (start, end) in enumerate(zip(self.vertices, self.vertices[1:])):
            x, y, distance = evenly_spaced(start, end, spacing)
            keep = slice(None) if index == 0 else slice(1, None)
            xx.extend(x[keep])
            yy.extend(y[keep])
            travel.extend(distance[keep] + offset)
            segment.extend([index] * len(distance[keep]))
            offset += distance[-1]
        return np.asarray(xx), np.asarray(yy), np.asarray(travel), np.asarray(segment)


def strip_plot(colors, coverage, measures, ruler):
    w = 850
    strip = Image.new("RGB", (w, 142), "#eedfca")
    draw = ImageDraw.Draw(strip)
    left, right = 20, w - 21
    boundaries = np.rint(np.linspace(left, right + 1, len(colors) + 1)).astype(int)
    for i, palette in enumerate(colors):
        if coverage[i] < 0.5:
            palette = [202, 196, 188] if i % 2 == 0 else [145, 134, 119]
        low, high = boundaries[i], boundaries[i + 1] - 1
        if high >= low:
            draw.rectangle((low, 26, high, 86), fill=tuple(int(ch) for ch in palette))
    draw.text((left, 8), "TRAVERSE / SURFACE SAMPLING", fill="#3c3022")
    for at in np.linspace(0, 1, 6):
        ix = round(left + (right - left) * at)
        draw.line((ix, 89, ix, 99), fill="#574938")
        units = f"{at * measures * ruler.per_pixel:.1f}" if ruler else f"{at * measures:.0f}"
        draw.text((ix - min(len(units), 4) * 3, 107), units, fill="#3c3022")
    draw.text((right - 13, 125), "mm" if ruler else "px", fill="#5b4a36")
    return strip


def traverse(image, settings, correction, ruler):
    data = obj(settings, "折线取样")
    path = Path.read(data.get("points"), *image.size)
    spacing = number(data.get("spacing", 3), "采样间距", low=0.5, high=40)
    xx, yy, travel, segments = path.evenly_sample(spacing)
    pixels, coverage = image_samples(image, xx, yy)
    adjusted = correction.apply(linearize(pixels * 255))
    display = encode(adjusted)
    labs = cielab(adjusted)
    result = []
    for segment in range(len(path.vertices) - 1):
        kmask = segments == segment
        valid = kmask & (coverage >= 0.5)
        weights = coverage[valid]
        if np.count_nonzero(valid):
            average = weighted_means(labs[valid], weights)
            spread = round(float(np.max(delta_e(labs[valid], average))), 4)
        else:
            average = None
            spread = None
        value = {
            "segment": segment + 1, "ends": [list(p) for p in path.vertices[segment:segment + 2]],
            "count": int(kmask.sum()), "valid_count": int(valid.sum()), "spread_delta_e": spread,
            "average_lab": [round(float(x), 4) for x in average] if average is not None else None,
        }
        if ruler is not None:
            value["length_mm"] = round(math.dist(*path.vertices[segment:segment + 2]) * ruler.per_pixel, 4)
        result.append(value)
    return {
        "segments": result, "count": len(segments), "length_px": round(path.total, 4),
        "length_mm": round(path.total * ruler.per_pixel, 4) if ruler else None,
        "coverage_fraction": round(float((coverage >= 0.5).mean()), 5),
    }, strip_plot(display, coverage, path.total, ruler)


def trim_sample(image, settings, ruler):
    raw = obj(settings, "样片裁切")
    style = raw.get("style", "region")
    if style == "rotated":
        center = coordinate(raw.get("center"), "裁片中心")
        width = integer(raw.get("width"), "裁片宽", low=4, high=1800)
        height = integer(raw.get("height"), "裁片高", low=4, high=1800)
        degrees = number(raw.get("angle", 0), "裁片转角", low=-180, high=180)
        result = rotated_crop(image, center, (width, height), degrees)
        alpha = np.asarray(result.getchannel("A"), dtype=np.float32)
        if not np.any(alpha):
            raise InputError("裁片没有留下原图像素")
        details = {
            "output_size": [width, height], "pixels_retained": round(float(alpha.sum()) / 255, 2),
            "center": list(center), "angle": degrees,
        }
        unscaled = width, height
    elif style == "region":
        max_side = integer(raw.get("max_side", 900), "输出长边", low=100, high=1800)
        border = integer(raw.get("border", 0), "透明边", low=0, high=120)
        result, details = crop_region(image, raw.get("region"), max_side=max_side, border=border)
        bounds = Box.read(details["source_bounds"])
        unscaled = bounds.width, bounds.height
        details["extra_border"] = border
    else:
        raise InputError("未识别的裁切方式")
    flatten_output = flag(raw.get("flatten", False), "转为浅纸底")
    if flatten_output:
        result = flatten(result, (248, 241, 226))
    details["transparent"] = not flatten_output
    if ruler:
        details["source_size_mm"] = [round(value * ruler.per_pixel, 5) for value in unscaled]
    return details, result


def cell_measurements(image, settings, correction):
    raw = obj(settings, "一致性分格")
    area = Region.read(raw.get("region"), *image.size)
    if area.vertices:
        raise InputError("一致性分格需要矩形样区")
    region = area.bounds
    nx = integer(raw.get("columns", 3), "分格列数", low=2, high=8)
    ny = integer(raw.get("rows", 3), "分格行数", low=2, high=8)
    if region.width / nx < 4 or region.height / ny < 4:
        raise InputError("每个格子至少需要 4 x 4 像素")
    maximum = number(raw.get("tolerance", 5), "单格 ΔE 容差", low=0.5, high=40)
    last = analyze(image, region.as_list(), correction, bins=8)
    cells = []
    for row in range(ny):
        for column in range(nx):
            box = [
                region.left + column * region.width / nx,
                region.top + row * region.height / ny,
                region.left + (column + 1) * region.width / nx,
                region.top + (row + 1) * region.height / ny,
            ]
            sample = window(image, box)
            if sample.coverage < 4 or sample.coverage / sample.geometric_area < 0.3:
                values = {"swatch": None, "delta_e": None, "result": "insufficient"}
            else:
                linear = correction.apply(linearize(sample.rgb))
                baseline = weighted_means(cielab(linear), sample.weights)
                difference = float(delta_e(baseline, last["lab"]))
                color = encode(weighted_means(linear, sample.weights))
                values = {
                    "swatch": "#" + "".join(f"{int(channel):02x}" for channel in color),
                    "delta_e": round(difference, 5),
                    "result": "review" if difference > maximum else "within",
                }
            values.update(row=row, column=column, region=box, coverage_px=round(sample.coverage, 3))
            cells.append(values)
    crop, _ = crop_region(image, region.as_list(), max_side=1100)
    visible = crop.convert("RGBA")
    overlay = Image.new("RGBA", visible.size)
    draw = ImageDraw.Draw(overlay)
    for cell in cells:
        x0 = round(cell["column"] * visible.width / nx)
        y0 = round(cell["row"] * visible.height / ny)
        x1 = round((cell["column"] + 1) * visible.width / nx) - 1
        y1 = round((cell["row"] + 1) * visible.height / ny) - 1
        palette = {"review": "#fb9667", "within": "#80d0a1", "insufficient": "#dadee0"}
        color = palette[cell["result"]]
        draw.rectangle((x0, y0, x1, y1), outline=color, width=2)
        draw.text((x0 + 6, y0 + 6), f"{chr(65 + cell['row'])}{cell['column'] + 1}", fill=color, stroke_width=1)
    visible.alpha_composite(overlay)
    return {
        "reference_lab": last["lab"], "tolerance": maximum, "rows": ny, "columns": nx,
        "cells": cells, "review_count": sum(cell["result"] == "review" for cell in cells),
        "insufficient_count": sum(cell["result"] == "insufficient" for cell in cells),
    }, visible
