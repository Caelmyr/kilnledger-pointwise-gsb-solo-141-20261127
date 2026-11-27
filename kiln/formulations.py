"""Map glaze surface phases using visible reference samples."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

from .calibration import cielab, delta_e, encode, html_color, linearize, weighted_means, weighted_quantiles
from .geometry import Region
from .imaging import window
from .inputs import InputError, distinct, hex_color, number, obj, seq, text


MAX_PHASE_POINTS = 7
COLORS = ("#e89477", "#67b8aa", "#b18cce", "#dfb94c", "#789ac4", "#9ab079", "#cf88a0")


@dataclass(frozen=True)
class Phase:
    name: str
    reference: object
    lab: np.ndarray
    limit: float
    photo_color: str
    marker: tuple

    @classmethod
    def read(cls, raw, image, correction, index):
        data = obj(raw, "釉相参照")
        name = text(data.get("name"), "釉相名", limit=30)
        limit = number(data.get("limit", 18), "釉相最大色差", low=1, high=80)
        reference = data.get("region")
        if reference is None:
            raise InputError("每种釉相必须选实物小样区")
        section = window(image, reference, max_pixels=480_000).require_pixels(12)
        corrected = correction.apply(linearize(section.rgb))
        lab = cielab(corrected)
        center = weighted_means(lab, section.weights)
        deviations = delta_e(lab, center)
        high = weighted_quantiles(deviations, section.weights, [0.9])[0]
        if high > limit:
            raise InputError(f"「{name}」参照片色差跨度超出该釉相的接受范围")
        photo_color = html_color(encode(weighted_means(corrected, section.weights)))
        marker = hex_color(data.get("marker", COLORS[index]), "标记色")
        return cls(name, reference, center, limit, photo_color, marker)


def phase_winners(lab, phases, minimum_gap):
    shape = lab.shape[:2]
    shortest = np.full(shape, np.inf, dtype=np.float32)
    next_shortest = np.full(shape, np.inf, dtype=np.float32)
    chosen = np.full(shape, -1, dtype=np.int16)
    acceptable = np.zeros(shape, dtype=np.uint8)
    for ix, phase in enumerate(phases):
        distance = delta_e(lab, phase.lab.astype(np.float32))
        possible = distance <= phase.limit
        acceptable += possible
        closer = possible & (distance < shortest)
        next_shortest = np.where(closer, shortest, np.where(possible, np.minimum(next_shortest, distance), next_shortest))
        shortest = np.where(closer, distance, shortest)
        chosen = np.where(closer, ix, chosen)
    unknown = acceptable == 0
    dual = (acceptable >= 2) & ((next_shortest - np.where(unknown, 0, shortest)) < minimum_gap)
    chosen[dual] = -2
    return chosen, shortest, acceptable


def island_details(chosen, phase_index, weights, left, top, minimum_area):
    labels, n = ndimage.label(chosen == phase_index)
    if n > 32_000:
        raise InputError("釉相结果过碎，请扩大参照色差的分辨间距")
    mass = np.bincount(labels.ravel(), weights=weights.ravel())
    accepted = mass >= minimum_area
    accepted[0] = False
    patch = accepted[labels]
    center_points = []
    for index, section in enumerate(ndimage.find_objects(labels), 1):
        if not accepted[index] or section is None:
            continue
        part = labels[section] == index
        yy, xx = np.where(part)
        center_points.append({
            "bounds": [section[1].start + left, section[0].start + top,
                       section[1].stop + left, section[0].stop + top],
            "center_px": [round(float(xx.mean() + section[1].start + left + 0.5), 4),
                          round(float(yy.mean() + section[0].start + top + 0.5), 4)],
            "area_px": round(float(mass[index]), 4),
        })
    center_points.sort(key=lambda item: (-item["area_px"], item["bounds"]))
    return patch, center_points[:40], int(accepted.sum())


def phase_tint(sample, mask, phases, blend):
    rgb = sample.rgb.astype(np.float32)
    shown = rgb.copy()
    for index, phase in enumerate(phases):
        marked = mask == index
        shown[marked] = rgb[marked] * (1 - blend) + np.asarray(phase.marker) * blend
    yy, xx = np.ogrid[:sample.height, :sample.width]
    ambiguous = (mask == -2) & (((xx + yy) // 5) % 2 == 1)
    shown[ambiguous] = shown[ambiguous] * (1 - blend) + np.array([254, 221, 145]) * blend
    alpha = np.rint(sample.weights * 255).astype(np.uint8)
    pixels = np.dstack((np.rint(shown).astype(np.uint8), alpha))
    pixels[alpha == 0, :3] = 0
    return Image.fromarray(pixels, "RGBA")


def inspect(image, settings, correction, ruler):
    data = obj(settings, "釉相分布")
    refs = seq(data.get("phases"), "釉相参照", minimum=2, maximum=MAX_PHASE_POINTS)
    phases = [Phase.read(item, image, correction, index) for index, item in enumerate(refs)]
    distinct([item.name.casefold() for item in phases], "釉相名")
    region = Region.read(data.get("region"), *image.size)
    minimum_size = number(data.get("minimum_area", 8), "釉相连片面积", low=1, high=8000)
    minimum_gap = number(data.get("gap", 1.5), "参照最小差距", low=0.001, high=15)
    alpha = number(data.get("overlay", 0.55), "釉相叠色", low=0.1, high=0.95)
    sample = window(image, region.as_json(), max_pixels=1_500_000).require_pixels(24)
    patch_lab = cielab(correction.apply(linearize(sample.rgb)))
    labels, distances, choices = phase_winners(patch_lab, phases, minimum_gap)
    pixels = sample.weights >= 0.4
    labels[~pixels] = -1
    area = float(sample.weights[pixels].sum())
    if area < 16:
        raise InputError("照片实底不足以判读釉相")
    kept = labels.copy()
    results = []
    for index, phase in enumerate(phases):
        accepted, islands, count = island_details(labels, index, sample.weights, sample.left, sample.top, minimum_size)
        rejected = (labels == index) & ~accepted
        kept[rejected] = -1
        patch_area = float(sample.weights[accepted].sum(dtype=np.float64))
        spread = weighted_quantiles(distances[accepted], sample.weights[accepted], [0.9]) if patch_area else []
        result = {
            "name": phase.name, "reference_region": phase.reference,
            "ref_lab": [round(float(v), 4) for v in phase.lab], "limit": phase.limit,
            "swatch": phase.photo_color, "marker": html_color(phase.marker),
            "count": count, "islands": islands,
            "area_px": round(patch_area, 4), "fraction": round(patch_area / area, 6),
            "reference_delta_e_p90": round(float(spread[0]), 5) if len(spread) else None,
        }
        if ruler is not None:
            result["area_mm2"] = round(patch_area * ruler.per_pixel ** 2, 5)
        results.append(result)
    ambiguous = float(sample.weights[kept == -2].sum(dtype=np.float64))
    outside = float(sample.weights[pixels & (kept == -1)].sum(dtype=np.float64))
    return {
        "region": region.as_json(), "phases": results,
        "ambiguous_fraction": round(ambiguous / area, 6),
        "unclassified_fraction": round(outside / area, 6),
        "multi_candidate_fraction": round(float(sample.weights[(choices > 1) & pixels].sum()) / area, 6),
        "usable_px": round(area, 4), "origin": [sample.left, sample.top],
        "max_display_side": 1024,
    }, phase_tint(sample, kept, phases, alpha)


def distribution_plot(result):
    w = 760
    h = 65 + 53 * len(result["phases"])
    canvas = Image.new("RGB", (w, h), "#efe3ce")
    draw = ImageDraw.Draw(canvas)
    draw.text((36, 13), "GLAZE PHASE COVERAGE", fill="#4c3427")
    for index, item in enumerate(result["phases"], start=1):
        x0 = 88
        y0 = index * 48 - 12
        width = 610
        end = max(0, round(width * item["fraction"]))
        draw.rectangle((x0, y0, x0 + width, y0 + 25), fill="#ded0bc")
        draw.rectangle((36, y0, 67, y0 + 25), fill=item["swatch"])
        if end:
            draw.rectangle((x0, y0, x0 + end, y0 + 25), fill=item["marker"])
        draw.text((x0 + 4, y0 + 5), f"{item['fraction']:.1%}  /  {item['count']} islands", fill="#493928")
    label = f"AMBIGUOUS {result['ambiguous_fraction']:.1%}    UNCLASSIFIED {result['unclassified_fraction']:.1%}"
    draw.text((36, h - 24), label, fill="#6d5a47")
    return canvas
