"""Pinhole segmentation, glare evidence and masked texture measurements."""

import math

import numpy as np
from PIL import Image
from scipy import ndimage

from .calibration import cielab, encode, linearize, luminance, named_values, weighted_quantiles
from .geometry import evenly_spaced
from .imaging import image_samples, window
from .inputs import InputError, choice, coordinate, number, obj


FOUR = ndimage.generate_binary_structure(2, 1)
EIGHT = ndimage.generate_binary_structure(2, 2)


def mask_preview(sample, mask, tint, *, strength=0.62):
    pixels = sample.rgb.astype(np.float32)
    amount = np.asarray(mask, dtype=np.float32)[..., None] * strength
    rgb = np.rint(pixels * (1 - amount) + np.asarray(tint) * amount).astype(np.uint8)
    alpha = np.rint(sample.weights * 255).astype(np.uint8)
    rgba = np.dstack((rgb, alpha))
    rgba[alpha == 0, :3] = 0
    return Image.fromarray(rgba, "RGBA")


def adaptive_mean(values, weights, sigma):
    total_weight = ndimage.gaussian_filter(weights, sigma, mode="reflect", truncate=2.5)
    weighted = ndimage.gaussian_filter(values * weights, sigma, mode="reflect", truncate=2.5)
    reference = np.divide(weighted, total_weight, out=np.zeros_like(weighted), where=total_weight >= 0.01)
    return reference, total_weight


def mask_extent(labels, label, section):
    yy, xx = np.where(labels[section] == label)
    if not len(xx):
        return None
    off_y, off_x = section[0].start, section[1].start
    return xx + off_x, yy + off_y


def components(mask, score, sample, *, connectivity, low_area, high_area):
    structure = FOUR if connectivity == "four" else EIGHT
    labels, count = ndimage.label(mask, structure=structure)
    if not count:
        return [], np.zeros_like(mask), 0
    if count > 30_000:
        raise InputError("异常区域过多，请收窄样区或增加判定阈值")
    sizes = np.bincount(labels.ravel(), weights=sample.weights.ravel())
    kept = (sizes >= low_area) & (sizes <= high_area)
    kept[0] = False
    groups = []
    for index, section in enumerate(ndimage.find_objects(labels), start=1):
        if not kept[index] or section is None:
            continue
        xx, yy = mask_extent(labels, index, section)
        weight = sample.weights[yy, xx]
        size = float(sizes[index])
        center = (
            float(np.sum((xx + 0.5) * weight) / size) + sample.left,
            float(np.sum((yy + 0.5) * weight) / size) + sample.top,
        )
        groups.append({
            "centroid": [round(v, 3) for v in center],
            "bbox": [
                int(xx.min()) + sample.left,
                int(yy.min()) + sample.top,
                int(xx.max()) + sample.left + 1,
                int(yy.max()) + sample.top + 1,
            ],
            "area": round(size, 2),
            "equivalent_diameter_px": round(math.sqrt(4 * size / math.pi), 3),
            "max_contrast": round(float(np.max(score[yy, xx])), 5),
        })
    groups.sort(key=lambda item: (-item["max_contrast"], -item["area"], item["bbox"]))
    return groups, kept[labels], count


def mark_pinholes(image, region, settings, correction, unit_factor=None):
    data = obj(settings, "针孔参数")
    threshold = number(data.get("contrast", 0.11), "暗部对比", low=0.01, high=0.75)
    radius = number(data.get("neighborhood", 5), "参考半径", low=1, high=32)
    low = number(data.get("minimum_area", 3), "最小针孔面积", low=1, high=200)
    high = number(data.get("maximum_area", 110), "最大针孔面积", low=low, high=5000)
    connectivity = choice(data.get("connectivity", "eight"), "连通方式", ("four", "eight"))

    sample = window(image, region, max_pixels=1_800_000).require_pixels(12)
    adjusted = correction.apply(linearize(sample.rgb))
    lightness = luminance(adjusted)
    average, support = adaptive_mean(lightness, sample.weights, radius)
    score = np.maximum(0, average - lightness)
    center_valid = (sample.weights >= 0.5) & (support >= 0.45)
    candidates = (score >= threshold) & center_valid
    groups, highlighted, raw_count = components(
        candidates,
        score,
        sample,
        connectivity=connectivity,
        low_area=low,
        high_area=high,
    )
    if unit_factor is not None:
        for group in groups:
            group["diameter_mm"] = round(group["equivalent_diameter_px"] * unit_factor, 3)
    result = {
        "spots": groups[:150],
        "count": len(groups),
        "area_px": round(float(sample.weights[highlighted].sum()), 2),
        "pre_filter_count": raw_count,
        "coverage_px": round(sample.coverage, 2),
        "origin": [sample.left, sample.top],
        "size": [sample.width, sample.height],
        "threshold": threshold,
        "neighborhood_px": radius,
        "unit_mm_per_px": unit_factor,
    }
    return result, mask_preview(sample, highlighted, (255, 92, 80))


def mark_glare(image, region, settings, correction):
    data = obj(settings, "高光参数")
    brightness = number(data.get("brightness", 0.81), "高光亮度", low=0.2, high=0.99)
    max_chroma = number(data.get("max_chroma", 14), "高光最大彩度", low=1, high=80)
    context = number(data.get("contrast", 0.08), "高光局部对比", low=0, high=0.5)
    radius = number(data.get("neighborhood", 10), "高光周边半径", low=1, high=24)
    sample = window(image, region, max_pixels=1_800_000).require_pixels(12)
    adjusted = correction.apply(linearize(sample.rgb))
    lab = cielab(adjusted)
    luma = luminance(adjusted)
    average, support = adaptive_mean(luma, sample.weights, radius)
    chroma = np.linalg.norm(lab[:, :, 1:], axis=-1)
    score = np.maximum(luma - average, 0)
    mask = (
        (luma >= brightness)
        & (chroma <= max_chroma)
        & (score >= context)
        & (support >= 0.3)
        & (sample.weights >= 0.5)
    )
    groups, highlighted, raw_count = components(
        mask, score, sample, connectivity="eight", low_area=2, high_area=sample.rgb.shape[0] * sample.rgb.shape[1]
    )
    marked = float(sample.weights[highlighted].sum())
    return {
        "fraction": round(marked / sample.coverage, 6),
        "coverage_px": round(sample.coverage, 2),
        "marked_px": round(marked, 2),
        "regions": groups[:80],
        "component_count": raw_count,
        "origin": [sample.left, sample.top],
        "size": [sample.width, sample.height],
    }, mask_preview(sample, highlighted, (83, 200, 250))


def texture(image, region, raw_settings, correction):
    raw = obj(raw_settings, "质地参数")
    scales = [
        number(raw.get("fine", 1.3), "细尺度", low=0.6, high=10),
        number(raw.get("coarse", 5.5), "粗尺度", low=1, high=25),
    ]
    if scales[0] >= scales[1]:
        raise InputError("粗尺度需要大于细尺度")
    sample = window(image, region, max_pixels=1_800_000).require_pixels(12)
    luma = luminance(correction.apply(linearize(sample.rgb)))
    small, small_support = adaptive_mean(luma, sample.weights, scales[0])
    large, large_support = adaptive_mean(luma, sample.weights, scales[1])
    weights = sample.weights * (small_support > 0.65) * (large_support > 0.65)
    if float(weights.sum()) < 8:
        raise InputError("样区实底面积过少，无法比较质地")
    fine = np.abs(luma - small)
    coarse = np.abs(small - large)
    results = []
    total = float(weights.sum())
    for scale, energy in zip(scales, (fine, coarse)):
        values = weighted_quantiles(energy, weights, [0.1, 0.5, 0.9])
        rms = np.sqrt((energy ** 2 * weights).sum(dtype=np.float64) / total)
        results.append({
            "radius_px": scale,
            "energy_rms": round(float(rms), 6),
            "percentiles": named_values(values),
        })
    heat = np.clip((fine + coarse) * 6, 0, 1) * (weights > 0)
    return {
        "scales": results,
        "usable_px": round(total, 2),
        "origin": [sample.left, sample.top],
        "size": [sample.width, sample.height],
    }, mask_preview(sample, heat, (248, 182, 61), strength=0.92)


def line_measure(image, settings, correction, ruler=None):
    data = obj(settings, "测线")
    start = coordinate(data.get("start"), "起点")
    end = coordinate(data.get("end"), "终点")
    for x, y in (start, end):
        if not 0 <= x <= image.width or not 0 <= y <= image.height:
            raise InputError("测线端点必须位于照片内")
    if math.dist(start, end) < 1:
        raise InputError("测线长度至少一个像素")
    dx = number(data.get("spacing", 3), "采样步长", low=0.5, high=90)
    xx, yy, travel = evenly_spaced(start, end, dx)
    rgb, alpha = image_samples(image, xx, yy)
    adjusted = correction.apply(linearize(rgb * 255))
    light = luminance(adjusted)
    encoded = encode(adjusted)
    entries = []
    for i, distance in enumerate(travel):
        valid = alpha[i] >= 0.5
        entry = {
            "x": round(float(xx[i]), 4),
            "y": round(float(yy[i]), 4),
            "distance_px": round(float(distance), 4),
            "coverage": round(float(alpha[i]), 4),
            "luma": round(float(light[i]), 6) if valid else None,
            "rgb": encoded[i].tolist() if valid else None,
        }
        if ruler is not None:
            entry["distance_mm"] = round(float(distance * ruler.per_pixel), 4)
        entries.append(entry)
    reliable = [item["luma"] for item in entries if item["luma"] is not None]
    return {
        "points": entries,
        "valid_count": len(reliable),
        "luma_range": [min(reliable), max(reliable)] if reliable else None,
        "length_px": round(float(travel[-1]), 5),
        "length_mm": round(float(travel[-1] * ruler.per_pixel), 5) if ruler else None,
    }


def aperture_check(image, region, settings):
    data = obj(settings, "遮挡检查")
    threshold = number(data.get("alpha_threshold", 0.95), "有效像素阈值", low=0.1, high=1)
    sample = window(image, region, max_pixels=2_000_000)
    support = sample.weights > 0
    solid = sample.weights >= threshold
    if solid.any():
        distance = ndimage.distance_transform_edt(np.pad(solid, 1))[1:-1, 1:-1]
        deepest = np.unravel_index(int(np.argmax(distance)), distance.shape)
        interior = {
            "radius_px": round(float(distance[deepest]), 4),
            "point": [float(deepest[1] + sample.left + 0.5), float(deepest[0] + sample.top + 0.5)],
        }
    else:
        interior = None
    return {
        "geometric_area": round(sample.geometric_area, 2),
        "covered_area": round(sample.coverage, 2),
        "solid_count": int(solid.sum()),
        "partial_count": int((support & ~solid).sum()),
        "interior_disk": interior,
        "origin": [sample.left, sample.top],
    }, mask_preview(sample, support & ~solid, (250, 95, 120))
