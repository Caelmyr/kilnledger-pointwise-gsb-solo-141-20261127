"""Prepare evidence-preserving masks and erase known non-specimen obstructions."""

from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import ndimage

from .calibration import encode, linearize, luminance
from .geometry import Region, center_mesh, polygon, polygon_hits, vertex_bounds
from .imaging import MAX_OPERATION_PIXELS, window
from .inputs import InputError, choice, coordinate, integer, number, obj, seq


@dataclass(frozen=True)
class Stroke:
    points: tuple
    shape: str
    action: str
    radius: float
    effect: float

    @classmethod
    def read(cls, raw, width, height):
        data = obj(raw, "遮挡编辑")
        shape = choice(data.get("shape", "polygon"), "遮挡形状", ("polygon", "brush"))
        action = choice(data.get("action", "erase"), "遮挡操作", ("erase", "restore"))
        radius = 0
        if shape == "polygon":
            points = polygon(data.get("points"), "遮挡轮廓", max_points=30)
        else:
            values = seq(data.get("points"), "笔迹位置", minimum=1, maximum=25)
            points = tuple(coordinate(item, "笔迹坐标") for item in values)
            radius = number(data.get("radius", 12), "笔刷半径", low=1, high=160)
        effect = number(data.get("strength", 1), "遮挡强度", low=0.01, high=1)
        for x, y in points:
            if not 0 <= x <= width or not 0 <= y <= height:
                raise InputError("遮挡编辑范围超出照片")
        return cls(points, shape, action, radius, effect)

    def bounds(self):
        result = vertex_bounds(self.points)
        if self.shape == "brush":
            result = result.expand(self.radius)
        return result

    def payload(self):
        return {
            "points": [list(point) for point in self.points], "shape": self.shape,
            "action": self.action, "radius": self.radius, "strength": self.effect,
        }


def squared_distance(xx, yy, start, stop):
    dx, dy = stop[0] - start[0], stop[1] - start[1]
    back, up = xx - start[0], yy - start[1]
    norm = dx ** 2 + dy ** 2
    if norm == 0:
        return back ** 2 + up ** 2
    t = np.clip((back * dx + up * dy) / norm, 0, 1)
    return (back - t * dx) ** 2 + (up - t * dy) ** 2


def brush_hits(points, xx, yy, radius):
    cutoff = radius ** 2
    if len(points) == 1:
        x0, y0 = points[0]
        return (xx - x0) ** 2 + (yy - y0) ** 2 <= cutoff
    area = np.zeros(np.broadcast_shapes(xx.shape, yy.shape), dtype=bool)
    for start, stop in zip(points, points[1:]):
        area |= squared_distance(xx, yy, start, stop) <= cutoff
    return area


def stroke_coverage(stroke, left, top, width, height):
    xx, yy = center_mesh(left, top, width, height)
    values = np.zeros((height, width), dtype=np.float32)
    for d1, d2 in ((-0.25, -0.25), (0.25, -0.25), (-0.25, 0.25), (0.25, 0.25)):
        if stroke.shape == "polygon":
            hits = polygon_hits(stroke.points, xx + d1, yy + d2)
        else:
            hits = brush_hits(stroke.points, xx + d1, yy + d2, stroke.radius)
        values += hits
    return values * (stroke.effect / 4)


def apply_mask(image, strokes):
    if image.width * image.height > MAX_OPERATION_PIXELS:
        raise InputError("遮挡编辑需要 350 万像素以内的图像")
    rgba = np.array(image, dtype=np.uint8)
    source = rgba[:, :, 3].astype(np.float32) / 255
    live = source.copy()
    edits = []
    for index, stroke in enumerate(strokes, 1):
        left, top, right, bottom = stroke.bounds().integer_extent(*image.size)
        if left >= right or top >= bottom:
            continue
        patch = live[top:bottom, left:right]
        original = source[top:bottom, left:right]
        coverage = stroke_coverage(stroke, left, top, right - left, bottom - top)
        target = original if stroke.action == "restore" else 0
        change = (target - patch) * coverage
        patch += change
        edits.append({
            "order": index, "change_px": round(float(change.sum(dtype=np.float64)), 4),
            "touched_px": int((coverage > 0).sum()), "extent": [left, top, right, bottom],
        })
    rgba[:, :, 3] = np.rint(live * 255).astype(np.uint8)
    rgba[rgba[:, :, 3] == 0, :3] = 0
    return {
        "source_coverage_px": round(float(source.sum(dtype=np.float64)), 3),
        "output_coverage_px": round(float(live.sum(dtype=np.float64)), 3),
        "lost_coverage_px": round(float((source - live).sum(dtype=np.float64)), 3),
        "edits": edits, "strokes": [stroke.payload() for stroke in strokes],
    }, Image.fromarray(rgba, "RGBA")


def remove_surroundings(image, settings):
    data = obj(settings, "层遮挡编辑")
    strokes = [Stroke.read(entry, *image.size)
               for entry in seq(data.get("strokes"), "遮挡笔迹", minimum=1, maximum=18)]
    return apply_mask(image, strokes)


def supports(luma, alpha, threshold, expansion):
    boundary = (alpha >= 0.55) & (luma >= threshold)
    if expansion:
        expanded = ndimage.binary_dilation(boundary, iterations=expansion, border_value=0)
    else:
        expanded = boundary
    return expanded & (alpha >= 0.55)


def normalized_blur(rgb, weights, sigma):
    density = ndimage.gaussian_filter(weights, sigma, truncate=3, mode="reflect")
    patch = np.empty(rgb.shape, dtype=np.float32)
    for index in range(3):
        weighted = ndimage.gaussian_filter(rgb[:, :, index] * weights, sigma, truncate=3, mode="reflect")
        patch[:, :, index] = np.divide(weighted, density, out=np.zeros_like(weighted), where=density > 0.05)
    return patch, density


def protect_near_edge(weight, mask, minimum_width):
    if minimum_width <= 1:
        return mask
    clean = np.pad(weight >= 0.55, 1, constant_values=False)
    distance = ndimage.distance_transform_edt(clean)[1:-1, 1:-1]
    return mask & (distance >= minimum_width)


def tame_glare(image, settings, correction):
    data = obj(settings, "局部高光压低")
    region = Region.read(data.get("region"), *image.size)
    sample = window(image, region.as_json(), max_pixels=1_500_000).require_pixels(24)
    strength = number(data.get("strength", 0.6), "压低程度", low=0.05, high=1)
    limit = number(data.get("threshold", 0.85), "高光阈值", low=0.42, high=0.99)
    brush = number(data.get("radius", 9), "补色半径", low=1.5, high=36)
    edge = number(data.get("protect", 3), "边缘保护宽", low=0, high=32)
    dilate = integer(data.get("expand", 1), "高光扩展", high=10)
    linear = correction.apply(linearize(sample.rgb))
    light = luminance(linear)
    candidate = supports(light, sample.weights, limit, dilate)
    accepted = protect_near_edge(sample.weights, candidate, edge)
    anchors = sample.weights * ~candidate
    recolor, confidence = normalized_blur(linear, anchors, brush)
    modifiable = accepted & (confidence > 0.11)
    weight = strength * sample.weights * modifiable
    mixed = linear * (1 - weight[..., None]) + recolor * weight[..., None]
    for channel in range(3):
        mixed[:, :, channel] = np.where(modifiable, mixed[:, :, channel], linear[:, :, channel])
    rgba = np.dstack((encode(mixed), np.rint(sample.weights * 255).astype(np.uint8)))
    rgba[rgba[:, :, 3] == 0, :3] = 0
    baseline = int(candidate.sum())
    revised = int(modifiable.sum())
    peak = float(np.max(light[modifiable])) if revised else None
    return {
        "region": region.as_json(), "candidate_px": baseline, "adjusted_px": revised,
        "protected_pixel_count": baseline - revised,
        "peak_original": round(peak, 5) if peak is not None else None,
        "maximum_changed": round(float(np.abs(mixed - linear).max()), 5),
        "mode": "local-estimate", "calibrated": bool(correction.samples),
    }, Image.fromarray(rgba, "RGBA")


def two_level_otsu(values, weights):
    frequency, edges = np.histogram(values, bins=128, range=(0, 1), weights=weights.astype(np.float64))
    total = frequency.sum()
    mean_bin = (edges[:-1] + edges[1:]) / 2
    between_mass = np.cumsum(frequency)
    between_mean = np.cumsum(frequency * mean_bin)
    global_mean = between_mean[-1] / total
    below = between_mass / total
    spread = np.divide(
        (global_mean * below - between_mean / total) ** 2,
        below * (1 - below),
        out=np.full_like(below, -1),
        where=(below > 0.009) & (below < 0.991),
    )
    best = int(np.argmax(spread))
    if spread[best] < 0:
        raise InputError("样区亮度变化太少，无法自动区分釉面与底板")
    return float(edges[best + 1])


def material_mask(image, raw, correction):
    data = obj(raw, "试片透底")
    region = Region.read(data.get("region"), *image.size)
    polarity = choice(data.get("polarity", "dark"), "釉面亮度方向", ("dark", "light"))
    remove_specks = integer(data.get("minimum_island", 18), "最小实物区面积", low=1, high=5000)
    fill_holes = integer(data.get("fill_holes", 0), "填补孔面积上限", low=0, high=3000)
    feather = number(data.get("feather", 0.7), "轮廓柔边", low=0, high=7)
    sample = window(image, region.as_json(), max_pixels=1_600_000).require_pixels(30)
    luminosity = luminance(correction.apply(linearize(sample.rgb)))
    level = data.get("level")
    level = two_level_otsu(luminosity, sample.weights) if level is None else number(level, "手工分界", low=0.01, high=0.99)
    if polarity == "dark":
        object_mask = (luminosity < level) & (sample.weights >= 0.5)
    else:
        object_mask = (luminosity >= level) & (sample.weights >= 0.5)
    labels, number_islands = ndimage.label(object_mask)
    frequency = np.bincount(labels.ravel())
    accepted_ids = frequency >= remove_specks
    accepted_ids[0] = False
    clean = accepted_ids[labels]
    if fill_holes:
        internal = ndimage.binary_fill_holes(clean) & ~clean & (sample.weights >= 0.5)
        holes, _ = ndimage.label(internal)
        areas = np.bincount(holes.ravel())
        fill = areas <= fill_holes
        fill[0] = False
        clean |= fill[holes]
    smoothed = clean.astype(np.float32)
    if feather:
        smoothed = ndimage.gaussian_filter(smoothed, feather, mode="constant", cval=0)
    alpha = np.rint(sample.weights * smoothed * 255).astype(np.uint8)
    channels = np.dstack((sample.rgb, alpha))
    channels[alpha == 0, :3] = 0
    counts = {
        "threshold": round(level, 6), "object_cells": int(clean.sum()),
        "recognized_islands": int(accepted_ids.sum()), "raw_islands": int(number_islands),
        "output_coverage_px": round(float(alpha.sum(dtype=np.float64)) / 255, 3),
        "origin": [sample.left, sample.top], "source_region": region.as_json(),
    }
    return counts, Image.fromarray(channels, "RGBA")
