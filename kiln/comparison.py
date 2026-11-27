"""Compare registered glaze photographs within explicit photographed regions."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image
from scipy import ndimage

from .calibration import cielab, delta_e, linearize, luminance, named_values, weighted_quantiles
from .geometry import Affine, Region, center_mesh
from .imaging import image_samples
from .inputs import InputError, coordinate, number, obj, seq
from .surface import EIGHT


MAX_COMPARE_SIDE = 1200


@dataclass(frozen=True)
class Anchors:
    points: tuple
    size: tuple

    @classmethod
    def read(cls, value, image, name):
        raw = seq(value, name, minimum=3, maximum=3)
        points = tuple(coordinate(point, name) for point in raw)
        for x, y in points:
            if not 0 <= x <= image.width or not 0 <= y <= image.height:
                raise InputError(f"{name}超出照片")
        transform = Affine.axes(*points)
        a, b = math.hypot(transform.ux, transform.uy), math.hypot(transform.vx, transform.vy)
        if min(a, b) < 8:
            raise InputError(f"{name}的两条参照边都需要至少 8 像素")
        return cls(points, (a, b))

    @property
    def affine(self):
        return Affine.axes(*self.points)

    def extent(self):
        return [list(point) for point in self.points]


@dataclass(frozen=True)
class Pair:
    lhs: np.ndarray
    rhs: np.ndarray
    left_support: np.ndarray
    right_support: np.ndarray
    x: np.ndarray
    y: np.ndarray
    bounds: object
    transform: Affine

    @property
    def usable(self):
        return np.minimum(self.left_support, self.right_support)


def difference_pair(left, right, data):
    a = Anchors.read(data.get("left_points"), left, "基准三点")
    b = Anchors.read(data.get("right_points"), right, "对照三点")
    current = Region.read(data.get("region"), *left.size)
    bounds = current.bounds
    scale = min(1, MAX_COMPARE_SIDE / max(bounds.width, bounds.height))
    width = max(1, round(bounds.width * scale))
    height = max(1, round(bounds.height * scale))
    xx, yy = center_mesh(0, 0, width, height)
    xx = bounds.left + xx * bounds.width / width
    yy = bounds.top + yy * bounds.height / height
    left_values, left_support = image_samples(left, xx, yy)
    ax, ay = a.affine.inverse(xx, yy)
    bx, by = b.affine.point(ax, ay)
    right_values, right_support = image_samples(right, bx, by)
    if current.vertices is not None:
        from .geometry import polygon_hits
        keep = polygon_hits(current.vertices, xx, yy)
        left_support *= keep
        right_support *= keep
    return Pair(left_values, right_values, left_support, right_support, xx, yy, bounds, b.affine), a, b


def residual_regions(mask, difference, pair, min_area):
    labeled, num = ndimage.label(mask, structure=EIGHT)
    if num > 25_000:
        raise InputError("差图碎片超过可测数量，请提高 ΔE 阈值")
    accepted = np.zeros(num + 1, dtype=bool)
    area = pair.bounds.width * pair.bounds.height / difference.size
    groups = []
    for i, section in enumerate(ndimage.find_objects(labeled), 1):
        if section is None:
            continue
        use = labeled[section] == i
        pixels = int(use.sum())
        if pixels * area < min_area:
            continue
        accepted[i] = True
        yy, xx = np.nonzero(use)
        x = xx + section[1].start
        y = yy + section[0].start
        horizontal, vertical = pair.x[0, x], pair.y[y, 0]
        center = [float(horizontal.mean()), float(vertical.mean())]
        groups.append({
            "approx_px": round(pixels * area, 3),
            "delta_e_peak": round(float(difference[section][use].max()), 4),
            "base_center": [round(value, 3) for value in center],
            "bounds": [float(horizontal.min()), float(vertical.min()),
                       float(horizontal.max()), float(vertical.max())],
        })
    groups.sort(key=lambda region: (-region["approx_px"], -region["delta_e_peak"], region["base_center"]))
    return groups[:100], int(np.count_nonzero(accepted)), accepted[labeled]


def comparison_image(pair, distance, highlighted, cutoff):
    baseline = np.rint(pair.lhs * 255).astype(np.uint8)
    value = np.clip(distance / max(cutoff, 5) / 3, 0, 1)
    tint = np.stack((
        227 + 28 * value,
        174 - 139 * value,
        59 + 98 * value,
    ), axis=-1)
    mix = np.where(highlighted, 0.43 + 0.43 * value, 0.0)[..., None]
    pixels = np.rint(baseline * (1 - mix) + tint * mix).astype(np.uint8)
    valid = pair.usable >= 0.5
    left_only = (pair.left_support >= 0.5) & ~valid
    xx = np.arange(pixels.shape[1], dtype=int)[None, :]
    yy = np.arange(pixels.shape[0], dtype=int)[:, None]
    stripe = left_only & (((xx + yy) // 6) % 2 == 0)
    pixels[stripe] = [67, 69, 78]
    alpha = np.rint(pair.left_support * 255).astype(np.uint8)
    pixels[alpha == 0] = 0
    return Image.fromarray(np.dstack((pixels, alpha)), "RGBA")


def compare(left, right, left_model, right_model, settings):
    data = obj(settings, "共视对比设置")
    cutoff = number(data.get("threshold", 8), "ΔE 阈值", low=0.5, high=90)
    min_area = number(data.get("minimum_area", 12), "最小片区", low=1, high=8000)
    pair, a, b = difference_pair(left, right, data)
    weights = pair.usable * (pair.usable >= 0.5)
    support = float(weights.sum())
    if support < 20:
        raise InputError("对齐照片不足 20 个采样像素的共有实底")
    left_linear = left_model.apply(linearize(pair.lhs * 255))
    right_linear = right_model.apply(linearize(pair.rhs * 255))
    distance = delta_e(cielab(left_linear), cielab(right_linear))
    mask = (distance >= cutoff) & (weights > 0)
    groups, count, kept = residual_regions(mask, distance, pair, min_area)
    percentiles = weighted_quantiles(distance, weights, [0.1, 0.5, 0.9])
    shifted = luminance(right_linear) - luminance(left_linear)
    signs = weighted_quantiles(shifted, weights, [0.1, 0.5, 0.9])
    dx = pair.bounds.width / pair.lhs.shape[1]
    dy = pair.bounds.height / pair.lhs.shape[0]
    return {
        "base_points": a.extent(), "comparison_points": b.extent(),
        "base_region": pair.bounds.as_list(),
        "retained_resolution": [pair.lhs.shape[1], pair.lhs.shape[0]],
        "common_pixels_estimate": round(support * dx * dy, 3),
        "base_only_pixels_estimate": round(float(((pair.left_support >= 0.5) & (pair.right_support < 0.5)).sum()) * dx * dy, 3),
        "delta_e": named_values(percentiles),
        "luma_change": named_values(signs),
        "above_threshold_fraction": round(float(weights[mask].sum()) / support, 6),
        "accepted_region_count": count,
        "region_peaks": groups,
        "threshold": cutoff,
    }, comparison_image(pair, distance, kept, cutoff)


def blend_pair(left, right, settings):
    data = obj(settings, "叠片设置")
    mix = number(data.get("opacity", 0.5), "上层透明度", low=0, high=1)
    pair, left_extent, right_extent = difference_pair(left, right, data)
    seen = pair.right_support * mix
    output_alpha = seen + pair.left_support * (1 - seen)
    base = pair.lhs * (pair.left_support * (1 - seen))[..., None]
    blend = base + pair.rhs * seen[..., None]
    pixels = np.divide(blend, output_alpha[..., None], out=np.zeros_like(blend), where=output_alpha[..., None] > 0)
    picture = Image.fromarray(np.rint(np.dstack((pixels, output_alpha)) * 255).astype(np.uint8), "RGBA")
    return {
        "base_points": left_extent.extent(),
        "comparison_points": right_extent.extent(),
        "region": pair.bounds.as_list(),
        "mix": mix,
        "common_fraction": round(float(np.sum(pair.usable)) / pair.usable.size, 6),
    }, picture
