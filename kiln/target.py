"""Inspect capture targets before using a photograph for measurements."""

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from .calibration import cielab, delta_e, encode, html_color, linearize, luminance, weighted_means, weighted_quantiles
from .geometry import Region
from .imaging import flatten, thumbnail, window
from .inputs import InputError, choice, distinct, hex_color, number, obj, seq, text


@dataclass(frozen=True)
class Limits:
    max_shadow: float
    max_highlight: float
    max_spread: float
    min_support: float
    color_tolerance: float

    @classmethod
    def read(cls, data):
        return cls(
            number(data.get("shadow", 0.06), "最大暗端溢出", low=0, high=0.75),
            number(data.get("highlight", 0.06), "最大亮端溢出", low=0, high=0.75),
            number(data.get("spread", 16), "色片内最大色差", low=0.1, high=70),
            number(data.get("min_support", 0.8), "色片最小实底占比", low=0.1, high=1),
            number(data.get("tolerance", 11), "参考色允许误差", low=0.25, high=75),
        )


class Patch:
    def __init__(self, label, region, expected, mode):
        self.label = label
        self.region = region
        self.expected = expected
        self.mode = mode

    @classmethod
    def read(cls, raw, size):
        data = obj(raw, "参考表片")
        label = text(data.get("name"), "参考片名", limit=38)
        region = data.get("region")
        if region is None:
            raise InputError("每块表片必须选框")
        block = Region.read(region, *size)
        if block.bounds.width < 5 or block.bounds.height < 5:
            raise InputError("表片框每边需要至少五个像素")
        expected = hex_color(data["expected"], "表片色值") if data.get("expected") else None
        mode = choice(data.get("mode", "color"), "采集目标用途", ("black", "white", "neutral", "color"))
        return cls(label, block, expected, mode)

    def measurements(self, source, correction, limits):
        sample = window(source, self.region.as_json(), max_pixels=400_000)
        report = {
            "name": self.label, "mode": self.mode, "region": self.region.as_json(),
            "coverage_px": round(sample.coverage, 3),
            "coverage_fraction": round(sample.coverage / sample.geometric_area, 5),
            "flags": [],
        }
        if sample.coverage < 12:
            report["flags"].append("too_little_coverage")
            return report
        observed = sample.rgb
        weights = sample.weights
        corrected = correction.apply(linearize(observed))
        output_lab = cielab(corrected)
        output_mean = weighted_means(output_lab, weights)
        contrast = delta_e(output_lab, output_mean)
        channel_shadow = weighted_means((observed <= 4).astype(np.float32), weights)
        channel_glare = weighted_means((observed >= 251).astype(np.float32), weights)
        luma = luminance(corrected)
        spread = float(weighted_quantiles(contrast, weights, [0.9])[0])
        report.update({
            "channel_shadow_fraction": [round(float(x), 6) for x in channel_shadow],
            "channel_highlight_fraction": [round(float(x), 6) for x in channel_glare],
            "measured_rgb": html_color(encode(weighted_means(corrected, weights))),
            "lab": [round(float(x), 4) for x in output_mean], "spread_delta_e": round(spread, 4),
            "luma_p10_p50_p90": [round(float(x), 6) for x in weighted_quantiles(luma, weights, [0.1, 0.5, 0.9])],
        })
        flags = report["flags"]
        if report["coverage_fraction"] < limits.min_support:
            flags.append("partial_patch")
        if self.mode != "black" and max(channel_shadow) > limits.max_shadow:
            flags.append("shadow_clipped")
        if self.mode != "white" and max(channel_glare) > limits.max_highlight:
            flags.append("highlight_clipped")
        if spread > limits.max_spread:
            flags.append("nonuniform_patch")
        if self.mode == "neutral" and float(np.linalg.norm(output_mean[1:])) > limits.color_tolerance:
            flags.append("neutral_color_cast")
        if self.expected is not None:
            difference = float(delta_e(cielab(linearize(self.expected)), output_mean))
            report.update(expected_rgb=html_color(self.expected), target_delta_e=round(difference, 4))
            if difference > limits.color_tolerance:
                flags.append("reference_mismatch")
        return report


def ordered_lightness(patches, minimum):
    dark = [item for item in patches if item["mode"] == "black" and "luma_p10_p50_p90" in item]
    light = [item for item in patches if item["mode"] == "white" and "luma_p10_p50_p90" in item]
    if not dark or not light:
        return {"available": False, "margin": None, "below_minimum": None}
    brightest_dark = max(item["luma_p10_p50_p90"][2] for item in dark)
    dimmest_light = min(item["luma_p10_p50_p90"][0] for item in light)
    separation = dimmest_light - brightest_dark
    return {
        "available": True, "margin": round(separation, 5), "minimum": minimum,
        "below_minimum": separation < minimum,
        "dark_cap": brightest_dark, "light_floor": dimmest_light,
    }


def rectangles_on(source, patches):
    view = flatten(thumbnail(source, 1040), (232, 224, 211))
    ratio_x, ratio_y = view.width / source.width, view.height / source.height
    canvas = ImageDraw.Draw(view)
    for i, record in enumerate(patches, 1):
        shape = Region.read(record["region"], *source.size)
        vertices = shape.vertices or shape.bounds.vertices()
        points = [(round(x * ratio_x), round(y * ratio_y)) for x, y in vertices]
        color = "#fa9771" if record["flags"] else "#85d4af"
        canvas.line(points + points[:1], fill=color, width=3)
        x, y = points[0]
        label_box = (x, max(0, y - 17), x + 23, y)
        canvas.rectangle(label_box, fill="#30251a")
        canvas.text((x + 4, max(0, y - 16)), str(i), fill=color)
    return view


def check_targets(image, settings, correction):
    data = obj(settings, "采集基准检查")
    limits = Limits.read(data)
    patches = [Patch.read(raw, image.size) for raw in seq(data.get("targets"), "参考表片", minimum=1, maximum=12)]
    distinct([item.label.casefold() for item in patches], "参考表片名称")
    findings = [item.measurements(image, correction, limits) for item in patches]
    margin = number(data.get("separation", 0.26), "黑白最小分隔", low=0.02, high=0.9)
    steps = ordered_lightness(findings, margin)
    return {
        "patches": findings, "tone_steps": steps, "calibrated": bool(correction.samples),
        "warning_count": sum(len(item["flags"]) for item in findings),
        "passed": not steps["below_minimum"] and not any(item["flags"] for item in findings),
    }, rectangles_on(image, findings)


def target_plot(result):
    w = 610
    h = max(116, 38 + len(result["patches"]) * 32)
    image = Image.new("RGB", (w, h), "#efe4ce")
    draw = ImageDraw.Draw(image)
    draw.text((29, 9), "CAPTURE  /  LINEAR LIGHTNESS INTERVALS", fill="#4b3928")
    for i, item in enumerate(result["patches"], 1):
        y = 32 + (i - 1) * 32
        color = "#ab442d" if item["flags"] else "#367562"
        swatch = item.get("measured_rgb", "#c5b39e")
        draw.rectangle((29, y, 48, y + 21), fill=swatch)
        draw.text((52, y + 3), str(i), fill=color)
        left, right = 85, w - 31
        draw.line((left, y + 10, right, y + 10), fill="#c7b89e")
        if "luma_p10_p50_p90" in item:
            x0, mid, x1 = [left + (right - left) * v for v in item["luma_p10_p50_p90"]]
            draw.line((x0, y + 10, x1, y + 10), fill=color, width=3)
            draw.ellipse((mid - 3, y + 7, mid + 3, y + 13), fill=color)
    return image
