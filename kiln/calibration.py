"""Reference calibration, weighted color measurement and presentation swatches."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw

from .imaging import window
from .inputs import InputError, hex_color, number, obj, seq, text


LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
RGB_XYZ = np.array([
    [0.4124564, 0.3575761, 0.1804375],
    [0.2126729, 0.7151522, 0.0721750],
    [0.0193339, 0.1191920, 0.9503041],
], dtype=np.float32)
ILLUMINANT = np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
DELTA = 6 / 29


def linearize(rgb):
    v = np.asarray(rgb, dtype=np.float32) / 255.0
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def encode(rgb):
    v = np.clip(rgb, 0, 1)
    encoded = np.where(v <= 0.0031308, 12.92 * v, 1.055 * v ** (1 / 2.4) - 0.055)
    return np.rint(encoded * 255).astype(np.uint8)


def luminance(rgb_linear):
    return np.asarray(rgb_linear) @ LUMA


def cielab(rgb_linear):
    xyz = (np.asarray(rgb_linear) @ RGB_XYZ.T) / ILLUMINANT
    f = np.where(xyz > DELTA ** 3, np.cbrt(xyz), xyz / (3 * DELTA ** 2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])), axis=-1)


def delta_e(lab_a, lab_b):
    return np.linalg.norm(np.asarray(lab_a) - np.asarray(lab_b), axis=-1)


def weighted_means(values, weights):
    denominator = weights.sum(dtype=np.float64)
    weighted = values * weights[..., None] if values.ndim > weights.ndim else values * weights
    axes = tuple(range(weights.ndim))
    return np.sum(weighted, axis=axes, dtype=np.float64) / denominator


def weighted_quantiles(values, weights, fractions):
    edges = np.argsort(values.ravel(), kind="stable")
    w = weights.ravel()[edges]
    useful = w > 0
    vs = values.ravel()[edges][useful]
    weight = w[useful]
    cdf = np.cumsum(weight, dtype=np.float64)
    if not len(cdf):
        raise InputError("样区没有可统计的像素")
    ix = np.searchsorted(cdf, cdf[-1] * np.asarray(fractions), side="left")
    return vs[np.minimum(ix, len(vs) - 1)]


def named_values(values):
    return {name: round(float(value), 5) for name, value in zip(("p10", "p50", "p90"), values)}


def html_color(rgb):
    return "#" + "".join(f"{int(value):02x}" for value in rgb)


def reference_stats(image, region, *, min_coverage=12):
    sample = window(image, region).require_pixels(min_coverage)
    linear = linearize(sample.rgb)
    average = weighted_means(linear, sample.weights)
    saturation = ((sample.rgb <= 3) | (sample.rgb >= 252)).any(axis=2)
    clipped = weighted_means(saturation, sample.weights)
    deviation = np.sqrt(weighted_means((linear - average) ** 2, sample.weights))
    return sample, average, clipped, deviation


@dataclass(frozen=True)
class Correction:
    gains: tuple
    offsets: tuple
    samples: tuple

    @classmethod
    def identity(cls):
        return cls((1, 1, 1), (0, 0, 0), ())

    @classmethod
    def from_json(cls, raw):
        if raw is None:
            return cls.identity()
        data = obj(raw, "校色结果")
        fields = []
        for label in ("gains", "offsets"):
            parts = seq(data.get(label), label, minimum=3, maximum=3)
            fields.append(tuple(number(p, label, low=-2, high=15) for p in parts))
        return cls(*fields, tuple(data.get("samples", [])))

    def apply(self, linear):
        return np.clip(linear * np.array(self.gains, dtype=np.float32)
                       + np.array(self.offsets, dtype=np.float32), 0, 1)

    def to_json(self):
        return {
            "gains": [round(x, 8) for x in self.gains],
            "offsets": [round(x, 8) for x in self.offsets],
            "samples": list(self.samples),
        }


def fit_correction(image, references):
    samples = []
    targets = []
    measured = []
    for ix, value in enumerate(seq(references, "参考色片", minimum=1, maximum=6)):
        entry = obj(value, "参考色片")
        name = text(entry.get("name", f"参考{ix + 1}"), "色片名称", limit=36)
        expected = hex_color(entry.get("expected"), "预期色值")
        patch = entry.get("region")
        if patch is None:
            raise InputError("每块参考色片都需实际框选")
        section, average, clipped, deviation = reference_stats(image, patch)
        if clipped > 0.1:
            raise InputError(f"{name}有超过一成极亮或极暗像素，请另选参考")
        if np.any(deviation > 0.17):
            raise InputError(f"{name}内颜色跨度太大，请将样区收窄")
        targets.append(linearize(expected))
        measured.append(average)
        samples.append({
            "name": name,
            "expected": html_color(expected),
            "measured": html_color(encode(average)),
            "region": patch,
            "coverage": round(section.coverage, 2),
            "clipped_fraction": round(float(clipped), 5),
        })
    target_array = np.asarray(targets)
    observed = np.asarray(measured)
    if len(samples) == 1:
        if np.any(observed <= 0.004):
            raise InputError("参考色片太暗，无法校正各色通道")
        gains = target_array[0] / observed[0]
        offsets = np.zeros(3)
    else:
        gains, offsets = np.zeros(3), np.zeros(3)
        for channel in range(3):
            x = observed[:, channel]
            if np.ptp(x) < 0.03:
                raise InputError("多色片校准需要每个通道都有可分辨的深浅差")
            coefficients, _, _, _ = np.linalg.lstsq(
                np.column_stack((x, np.ones(len(x)))),
                target_array[:, channel],
                rcond=None,
            )
            gains[channel], offsets[channel] = coefficients
    if np.any(gains < 0.2) or np.any(gains > 5) or np.any(np.abs(offsets) > 0.5):
        raise InputError("参考色片给出的校正幅度不可信")
    result = Correction(tuple(gains), tuple(offsets), tuple(samples))
    transformed = result.apply(observed)
    for sample, predicted, target in zip(samples, transformed, target_array):
        sample["residual_delta_e"] = round(float(delta_e(cielab(predicted), cielab(target))), 3)
    return result


def corrected_image(image, correction):
    original = np.asarray(image, dtype=np.uint8)
    pixels = np.empty_like(original)
    for start in range(0, image.height, 256):
        portion = original[start:start + 256]
        pixels[start:start + 256, :, :3] = encode(correction.apply(linearize(portion[:, :, :3])))
    pixels[:, :, 3] = original[:, :, 3]
    pixels[pixels[:, :, 3] == 0, :3] = 0
    return Image.fromarray(pixels, "RGBA")


def histogram(values, weights, bins):
    mass, edges = np.histogram(values, range=(0, 1), bins=bins, weights=weights.astype(np.float64))
    return [
        {"start": round(float(low), 5), "end": round(float(high), 5), "fraction": round(float(count / mass.sum()), 7)}
        for low, high, count in zip(edges, edges[1:], mass)
    ]


def analyze(image, region, correction, *, bins=24):
    sample = window(image, region).require_pixels()
    linear = correction.apply(linearize(sample.rgb))
    rgb_mean = encode(weighted_means(linear, sample.weights))
    lab = cielab(linear)
    lab_mean = weighted_means(lab, sample.weights)
    luma = luminance(linear)
    lightness = weighted_quantiles(luma, sample.weights, [0.1, 0.5, 0.9])
    dispersion = weighted_quantiles(delta_e(lab, lab_mean), sample.weights, [0.1, 0.5, 0.9])
    return {
        "swatch": html_color(rgb_mean),
        "rgb": rgb_mean.tolist(),
        "lab": [round(float(x), 4) for x in lab_mean],
        "linear_luma": named_values(lightness),
        "delta_e_spread": named_values(dispersion),
        "coverage_px": round(sample.coverage, 2),
        "coverage_fraction": round(sample.coverage / sample.geometric_area, 5),
        "histogram": histogram(luma, sample.weights, bins),
        "window_origin": [sample.left, sample.top],
        "window_size": [sample.width, sample.height],
        "calibrated": bool(correction.samples),
    }


def palette_roles(image, entries, correction):
    measurements = []
    names = set()
    for raw in seq(entries, "色样区域", minimum=1, maximum=14):
        record = obj(raw, "色样区域")
        name = text(record.get("name"), "色样名称", limit=30)
        if name.casefold() in names:
            raise InputError("色样名不能重复")
        names.add(name.casefold())
        measured = analyze(image, record.get("region"), correction, bins=10)
        measurements.append({
            "name": name,
            "swatch": measured["swatch"],
            "lab": measured["lab"],
            "coverage_px": measured["coverage_px"],
            "delta_e_p90": measured["delta_e_spread"]["p90"],
        })
    return measurements


def histogram_image(result):
    width, height = 640, 270
    image = Image.new("RGB", (width, height), "#f7f3ea")
    canvas = ImageDraw.Draw(image)
    canvas.text((35, 18), "LINEAR LIGHTNESS DISTRIBUTION", fill="#303328")
    x0, x1 = 35, width - 34
    y0, y1 = 222, 44
    canvas.line((x0, y0, x1, y0), fill="#656657")
    bins = result["histogram"]
    maximum = max(item["fraction"] for item in bins)
    spacing = (x1 - x0) / len(bins)
    for i, item in enumerate(bins):
        left = round(x0 + spacing * i)
        right = round(x0 + spacing * (i + 1)) - 2
        top = y0 - math.ceil(item["fraction"] / maximum * (y0 - y1)) if maximum else y0
        canvas.rectangle((left, top, right, y0), fill=result["swatch"])
    for ix in range(5):
        x = x0 + (x1 - x0) * ix / 4
        canvas.text((x - 4, y0 + 9), str(ix / 4), fill="#555749")
    return image
