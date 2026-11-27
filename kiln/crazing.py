"""Directional dark ridges and length/branch summaries for crazed glazes."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image
from scipy import ndimage

from .calibration import linearize, luminance
from .imaging import window
from .inputs import InputError, flag, integer, number, obj
from .surface import EIGHT, FOUR, adaptive_mean, mask_preview


MAX_CRAZING_PIXELS = 1_140_000


@dataclass(frozen=True)
class RidgeSettings:
    floor: float
    sigma: float
    contrast: float
    minimum_size: int
    fill: bool
    radius: int

    @classmethod
    def read(cls, raw):
        data = obj(raw, "开片检视参数")
        return cls(
            number(data.get("floor", 0.018), "暗纹强度", low=0.002, high=0.23),
            number(data.get("scale", 1.45), "纹线宽度", low=0.8, high=7),
            number(data.get("contrast", 0.045), "明暗对比", low=0.005, high=0.6),
            integer(data.get("minimum", 12), "最小连片面积", low=3, high=2300),
            flag(data.get("bridge", True), "联接一个像素内的短间隙"),
            integer(data.get("edge", 2), "边界保护像素", low=0, high=18),
        )


def hessian_plane(data, sigma):
    kwargs = {"sigma": sigma, "mode": "nearest", "truncate": 3.2}
    scale = sigma ** 2
    xx = ndimage.gaussian_filter(data, order=(0, 2), **kwargs) * scale
    yy = ndimage.gaussian_filter(data, order=(2, 0), **kwargs) * scale
    xy = ndimage.gaussian_filter(data, order=(1, 1), **kwargs) * scale
    return xx, yy, xy


def line_strength(response):
    xx, yy, xy = response
    cross = np.sqrt((xx - yy) ** 2 + 4 * xy ** 2)
    major = (xx + yy + cross) / 2
    minor = (xx + yy - cross) / 2
    major = np.maximum(0, major)
    ratio = np.divide(np.abs(minor), major, out=np.ones_like(major), where=major > 1e-10)
    ridge = major * np.exp(-2.5 * ratio ** 2)
    angle = (0.5 * np.arctan2(2 * xy, xx - yy) + np.pi / 2) % np.pi
    return ridge, angle


def clean_components(candidate, size):
    labels, _ = ndimage.label(candidate, structure=EIGHT)
    hist = np.bincount(labels.ravel())
    passed = hist >= size
    passed[0] = False
    return passed[labels]


def directional_nonmax(strength, direction, limit):
    padded = np.pad(strength, 1)
    h, w = strength.shape
    keep = np.zeros_like(strength, dtype=bool)
    axis = np.rint((direction + np.pi / 2) / (np.pi / 4)).astype(np.int32) % 4
    for index, (dy, dx) in enumerate(((0, 1), (1, 1), (1, 0), (1, -1))):
        forward = padded[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        back = padded[1 - dy:1 - dy + h, 1 - dx:1 - dx + w]
        keep |= (axis == index) & (strength >= forward) & (strength > back)
    return keep & (strength >= limit)


def region_edges(mask):
    padded = np.pad(mask, 1)
    yy, xx = np.ogrid[1:mask.shape[0] + 1, 1:mask.shape[1] + 1]
    return sum(padded[yy + dy, xx + dx].astype(np.uint8)
               for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0))


def distance_lengths(mask):
    horiz = mask[:, 1:] & mask[:, :-1]
    vert = mask[1:, :] & mask[:-1, :]
    both = mask[1:, 1:] & mask[:-1, :-1]
    diag = mask[1:, :-1] & mask[:-1, 1:]
    both &= ~(mask[:-1, 1:] | mask[1:, :-1])
    diag &= ~(mask[:-1, :-1] | mask[1:, 1:])
    return float(horiz.sum() + vert.sum() + math.sqrt(2) * (both.sum() + diag.sum()))


def angular_profile(weights, angles):
    edges = np.linspace(0, np.pi, 9)
    hist, _ = np.histogram(angles, bins=edges, weights=weights)
    total = hist.sum()
    return [{"start_degrees": round(float(np.rad2deg(edge)), 3),
             "end_degrees": round(float(np.rad2deg(edges[index + 1])), 3),
             "fraction": round(float(hist[index] / total), 6) if total else 0}
            for index, edge in enumerate(edges[:-1])]


def branch_points(candidate, support):
    branch = (region_edges(candidate) >= 3) & candidate & support
    groups, count = ndimage.label(branch, structure=EIGHT)
    points = []
    for index, section in enumerate(ndimage.find_objects(groups), 1):
        if section is None:
            continue
        yy, xx = np.where(groups[section] == index)
        points.append({
            "x": float(xx.mean() + section[1].start + 0.5),
            "y": float(yy.mean() + section[0].start + 0.5),
            "local_nodes": len(xx),
        })
    points.sort(key=lambda point: -point["local_nodes"])
    return points[:80], count


def inspect(image, settings, correction, ruler):
    data = obj(settings, "开片检视")
    config = RidgeSettings.read(data)
    patch = window(image, data.get("region"), max_pixels=MAX_CRAZING_PIXELS).require_pixels(28)
    light = luminance(correction.apply(linearize(patch.rgb)))
    local, density = adaptive_mean(light, patch.weights, max(1.2, config.sigma * 2.6))
    solid = (patch.weights > 0.7) & (density > 0.57)
    protect = ndimage.binary_erosion(solid, FOUR, iterations=config.radius) if config.radius else solid
    background = np.where(solid, light, local)
    ridge, angles = line_strength(hessian_plane(background, config.sigma))
    dark = (local - light) >= config.contrast
    colored = directional_nonmax(ridge, angles, config.floor) & protect & dark
    if config.fill:
        colored |= ndimage.binary_closing(colored, structure=EIGHT) & protect & (ridge >= config.floor / 2)
    selection = clean_components(colored, config.minimum_size)
    length = distance_lengths(selection)
    branches, count = branch_points(selection, solid)
    points = [{"x": round(point["x"] + patch.left, 4), "y": round(point["y"] + patch.top, 4),
               "local_nodes": point["local_nodes"]} for point in branches]
    usable = float(patch.weights[protect].sum())
    if usable < 12:
        raise InputError("可判读实底过少，无法进行开片检视")
    result = {
        "line_pixels": int(selection.sum()), "centerline_px": round(length, 4),
        "protected_coverage_px": round(usable, 4), "node_groups": count, "junctions": points,
        "luma_direction_profile": angular_profile(ridge[selection], angles[selection]),
        "origin": [patch.left, patch.top], "output_size": [patch.width, patch.height],
    }
    if ruler is not None:
        result["summary_length_mm"] = round(length * ruler.per_pixel, 4)
        result["sample_area_mm2"] = round(usable * ruler.per_pixel ** 2, 4)
    return result, mask_preview(patch, selection, (119, 245, 183), strength=0.91)


def directionality_picture(result):
    axis = result["luma_direction_profile"]
    size = 338
    mid = size // 2
    graphic = Image.new("RGB", (size, size), "#ded4c3")
    from PIL import ImageDraw
    draw = ImageDraw.Draw(graphic)
    draw.ellipse((33, 33, size - 34, size - 34), outline="#a7977f")
    draw.line((25, mid, size - 26, mid), fill="#b3a38e")
    draw.line((mid, 25, mid, size - 26), fill="#b3a38e")
    largest = max(value["fraction"] for value in axis)
    for bar in axis:
        if not largest:
            break
        theta = math.radians((bar["start_degrees"] + bar["end_degrees"]) / 2)
        radius = (mid - 37) * bar["fraction"] / largest
        vx, vy = math.cos(theta) * radius, math.sin(theta) * radius
        draw.line((mid - vx, mid - vy, mid + vx, mid + vy), fill="#583722", width=4)
    draw.text((12, 12), "CRAZING ORIENTATION", fill="#4d3f30")
    return graphic
