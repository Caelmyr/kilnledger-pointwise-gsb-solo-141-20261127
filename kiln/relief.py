"""Inspect surface relief with two shots lit from opposite directions."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .calibration import linearize, luminance, named_values, weighted_means, weighted_quantiles
from .comparison import difference_pair
from .inputs import InputError, choice, flag, number, obj
from .surface import adaptive_mean


@dataclass(frozen=True)
class Setup:
    light_elevation: float
    contrast: float
    texture_radius: float
    saturation: float
    normalize: bool
    output: str

    @classmethod
    def read(cls, data):
        return cls(
            number(data.get("light_elevation", 37), "灯光离台角度", low=8, high=78),
            number(data.get("minimum_luma", 0.035), "双向可用亮度", low=0.004, high=0.45),
            number(data.get("radius", 18), "低频估计半径", low=2, high=50),
            number(data.get("saturation", 0.92), "排除过亮像素", low=0.5, high=0.999),
            flag(data.get("normalize", True), "消去两侧照明曝光比例"),
            choice(data.get("output", "tilt"), "浮凸检查图", ("tilt", "reflection", "confidence")),
        )


def normalize_lighting(first, second, support, optional):
    if not optional:
        return first, second, 1.0
    valid = (first > 0.05) & (second > 0.05) & (first < 0.8) & (second < 0.8)
    samples = support * valid
    if float(samples.sum()) <= 24:
        raise InputError("没有足够的中亮像素用于估计两次照明差")
    quotient = np.divide(first, second, out=np.ones_like(first), where=valid)
    ratio = float(weighted_quantiles(quotient, samples, [0.5])[0])
    if not 0.25 < ratio < 4.0:
        raise InputError("两侧亮度差异过大，请重拍或显式关闭曝光归一")
    gain = math.sqrt(ratio)
    return first / gain, second * gain, ratio


def slopes(first, second, support, setup):
    gate = (np.minimum(first, second) >= setup.contrast) & (np.maximum(first, second) < setup.saturation)
    usable = support * gate
    if float(usable.sum(dtype=np.float64)) < 24:
        raise InputError("同时有实底、未过亮且亮度足够的两照交集太小")
    sum_light = first + second
    normalized = np.divide(first - second, sum_light, out=np.zeros_like(first), where=gate)
    baseline, density = adaptive_mean(normalized, usable, setup.texture_radius)
    response = normalized - baseline
    quality = usable * (density > 0.12)
    if float(quality.sum(dtype=np.float64)) < 24:
        raise InputError("测量邻域实底不足以估计浮凸趋势")
    radians = np.arctan(response * math.tan(math.radians(setup.light_elevation)))
    slope = np.rad2deg(radians)
    symmetric = np.where(gate, sum_light / 2, 0)
    return slope, symmetric, gate, quality


def render_relief(base, tilt, reflection, quality, support, choice_name):
    rgb = base * 255
    if choice_name == "tilt":
        response = np.minimum(np.abs(tilt) / 15, 1)
        hot = np.array([241, 120, 74])
        cool = np.array([70, 173, 225])
        direction = np.where((tilt > 0)[..., None], hot, cool)
        amount = response[..., None] * 0.92
        color = rgb * (1 - amount) + direction * amount
    elif choice_name == "reflection":
        middle = np.sqrt(np.clip(reflection, 0, 1)) * 255
        color = np.repeat(middle[..., None], 3, axis=2)
    else:
        color = np.stack((250 - quality * 152, 135 + quality * 90, 94 + quality * 48), axis=-1)
    variable = np.where((quality >= 0.12)[..., None], color, rgb)
    yy, xx = np.ogrid[:rgb.shape[0], :rgb.shape[1]]
    missing = (quality < 0.12) & ((xx + yy) // 6 % 2 == 1)
    variable[missing] = [112, 85, 82]
    coverage = np.rint(np.clip(support, 0, 1) * 255).astype(np.uint8)
    channels = np.dstack((np.rint(variable).astype(np.uint8), coverage))
    channels[coverage == 0, :3] = 0
    return Image.fromarray(channels, "RGBA")


def suspect_groups(tilt, quality, anchor, threshold):
    demand = abs(tilt) >= threshold
    labels, count = ndimage.label(demand & (quality >= 0.2))
    if count > 20_000:
        raise InputError("浮凸提示图过碎，请提高角度阈值")
    xs, ys = anchor.x[0], anchor.y[:, 0]
    result = []
    area = np.bincount(labels.ravel(), weights=quality.ravel())
    for index, section in enumerate(ndimage.find_objects(labels), 1):
        if section is None or area[index] < 5:
            continue
        mask = labels[section] == index
        sloped = tilt[section][mask]
        yy, xx = np.where(mask)
        x = xs[xx + section[1].start]
        y = ys[yy + section[0].start]
        extreme = float(sloped[int(np.argmax(np.abs(sloped)))])
        result.append({
            "sample_columns": len(sloped), "extreme_degrees": round(extreme, 4),
            "origin": [round(float(x.mean()), 4), round(float(y.mean()), 4)],
            "bounds": [float(x.min()), float(y.min()), float(x.max()), float(y.max())],
        })
    result.sort(key=lambda entry: (-abs(entry["extreme_degrees"]), -entry["sample_columns"], entry["origin"]))
    return result[:80]


def surface_map(first, second, first_color, second_color, settings):
    data = obj(settings, "双向掠射图")
    setup = Setup.read(data)
    pair, start, end = difference_pair(first, second, data)
    left = luminance(first_color.apply(linearize(pair.lhs * 255)))
    right = luminance(second_color.apply(linearize(pair.rhs * 255)))
    valid = pair.usable >= 0.5
    support = pair.usable * valid
    na, nb, gain = normalize_lighting(left, right, support, setup.normalize)
    tilt, middle, gate, quality = slopes(na, nb, support, setup)
    threshold = number(data.get("slope_threshold", 6), "浮凸提示角度", low=0.5, high=30)
    suspect = suspect_groups(tilt, quality, pair, threshold)
    bands = weighted_quantiles(tilt, quality, [0.1, 0.5, 0.9])
    abs_bands = weighted_quantiles(abs(tilt), quality, [0.1, 0.5, 0.9])
    recovery = float(support.sum(dtype=np.float64))
    result = {
        "tilt_degrees": named_values(bands), "magnitude_degrees": named_values(abs_bands),
        "common_coverage_fraction": round(recovery / tilt.size, 5),
        "qualified_fraction_of_common": round(float(quality.sum(dtype=np.float64)) / recovery, 5),
        "saturated_or_dim_fraction_of_common": round(float(support[~gate].sum(dtype=np.float64)) / recovery, 5),
        "average_reflected_luma": round(float(weighted_means(middle, quality)), 5),
        "exposure_ratio_before": round(gain, 5),
        "source_points": [start.extent(), end.extent()], "regions": suspect,
        "elevation": setup.light_elevation, "threshold_degrees": threshold, "map_kind": setup.output,
        "approximation": "one-axis relative slope with constant opposite lighting",
    }
    image = render_relief(pair.lhs, tilt, middle, quality, pair.left_support, setup.output)
    return result, image


def preview_scale(result):
    width, height = 660, 140
    image = Image.new("RGB", (width, height), "#eee2ce")
    draw = ImageDraw.Draw(image)
    draw.text((26, 12), "OPPOSED LIGHT / RELATIVE TILT", fill="#493725")
    x0, x1 = 39, width - 43
    center = (x0 + x1) // 2
    for x in range(x0, x1 + 1):
        limit = (x - center) / (x1 - center)
        baseline = np.array([212, 198, 178])
        tint = np.array([236, 100, 63]) if limit > 0 else np.array([79, 156, 213])
        result_rgb = tuple(np.rint(baseline * (1 - abs(limit)) + tint * abs(limit)).astype(int))
        draw.line((x, 40, x, 99), fill=result_rgb)
    for text, x in (("-15 deg", x0), ("0", center), ("+15 deg", x1 - 28)):
        draw.text((x, 109), text, fill="#493725")
    return image


# The horizontal/vertical pair reconciliation lives in :mod:`kiln.heightnet`;
# re-export the common entry points from the grazing-light module too.
from .heightnet import (  # noqa: E402,F401
    classify_step,
    query_pair,
    solve_network,
    synthesize as synthesize_height_network,
)

height_network = solve_network
solve_height_map = solve_network
reconcile = solve_network
relative_height_range = query_pair
