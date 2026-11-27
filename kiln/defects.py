"""Trace visible surface defects and grade photographed glaze regions."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw

from .calibration import cielab, delta_e, linearize, weighted_means
from .geometry import Region, polygon, signed_area
from .imaging import crop_region, window
from .inputs import InputError, choice, coordinate, distinct, integer, number, obj, optional_text, seq, text


TYPES = ("crack", "crawling", "blister", "pinholes", "stain", "unknown")
OUTCOMES = ("observe", "test", "hold")
COLORS = {
    "crack": "#e55542",
    "crawling": "#f9bf48",
    "blister": "#5ab6e8",
    "pinholes": "#ad79e3",
    "stain": "#e79996",
    "unknown": "#96b6a2",
}


@dataclass(frozen=True)
class Mark:
    name: str
    kind: str
    outcome: str
    severity: int
    shape_type: str
    points: tuple
    note: str

    @classmethod
    def read(cls, value, width, height):
        raw = obj(value, "缺陷标注")
        name = text(raw.get("name"), "标注名", limit=50)
        kind = choice(raw.get("kind"), "缺陷类别", TYPES)
        outcome = choice(raw.get("outcome", "observe"), "后续操作", OUTCOMES)
        severity = integer(raw.get("severity", 1), "等级", low=1, high=4)
        shape = choice(raw.get("shape", "polygon"), "标注形状", ("polygon", "trace", "point"))
        limits = {"polygon": (3, 24), "trace": (2, 35), "point": (1, 1)}
        if shape == "polygon":
            points = polygon(raw.get("points"), "异常轮廓", max_points=limits[shape][1])
        else:
            low, high = limits[shape]
            line = seq(raw.get("points"), "异常位置", minimum=low, maximum=high)
            points = tuple(coordinate(point, "异常位置") for point in line)
            if len(set(points)) != len(points):
                raise InputError("重复的异常点")
        for x, y in points:
            if x < 0 or y < 0 or x > width or y > height:
                raise InputError("异常标注超出照片")
        return cls(name, kind, outcome, severity, shape, points, optional_text(raw.get("note"), "标注备注", 200))

    def measurements(self, ruler):
        length = 0
        area = 0
        if self.shape_type == "polygon":
            area = abs(signed_area(self.points))
            length = math.fsum(math.dist(a, b) for a, b in zip(self.points, self.points[1:] + self.points[:1]))
        elif self.shape_type == "trace":
            length = math.fsum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))
        result = {"length_px": round(length, 4), "area_px2": round(area, 4)}
        if ruler is not None:
            result["length_mm"] = round(length * ruler.per_pixel, 4)
            result["area_mm2"] = round(area * ruler.per_pixel ** 2, 4)
        return result

    def as_json(self, ruler=None):
        return {
            "name": self.name, "kind": self.kind, "severity": self.severity,
            "shape": self.shape_type, "points": [list(point) for point in self.points],
            "outcome": self.outcome, "note": self.note, "size": self.measurements(ruler),
        }


def draw_marks(image, marks):
    size = min(1, 1600 / max(image.size))
    base = image.resize(tuple(max(1, round(value * size)) for value in image.size), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", base.size)
    draw = ImageDraw.Draw(layer)
    for index, mark in enumerate(marks, start=1):
        path = [(x * size, y * size) for x, y in mark.points]
        color = COLORS[mark.kind]
        cx, cy = path[0]
        if mark.shape_type == "point":
            draw.ellipse((cx - 8, cy - 8, cx + 8, cy + 8), outline=color, width=3)
        elif mark.shape_type == "trace":
            draw.line(path, color, width=3, joint="curve")
        else:
            draw.polygon(path, fill=color + "44", outline=color, width=3)
        text_left, text_top = max(0, cx + 5), max(0, cy - 21)
        draw.rounded_rectangle((text_left, text_top, text_left + 23, text_top + 17), radius=3, fill="#211c16")
        draw.text((text_left + 4, text_top + 1), str(index), fill=color)
    base.alpha_composite(layer)
    return base


def marks_result(image, settings, ruler):
    data = obj(settings, "缺陷图")
    entries = seq(data.get("marks"), "标注", minimum=1, maximum=45)
    marks = [Mark.read(entry, *image.size) for entry in entries]
    distinct([mark.name.casefold() for mark in marks], "标注名")
    by_case = {name: 0 for name in TYPES}
    severity = {str(level): 0 for level in range(1, 5)}
    priorities = []
    for index, mark in enumerate(marks, 1):
        by_case[mark.kind] += 1
        severity[str(mark.severity)] += 1
        if mark.outcome in ("test", "hold"):
            priorities.append({"mark": index, "outcome": mark.outcome, "severity": mark.severity})
    priorities.sort(key=lambda item: (-item["severity"], item["mark"]))
    return {
        "marks": [mark.as_json(ruler) for mark in marks],
        "review_queue": priorities, "by_type": by_case, "by_severity": severity,
    }, draw_marks(image, marks)


def annular_sample(image, region, *, center, radius):
    sample = window(image, region).require_pixels(4)
    yy, xx = np.ogrid[sample.top:sample.top + sample.height, sample.left:sample.left + sample.width]
    distance = np.hypot(xx + 0.5 - center[0], yy + 0.5 - center[1])
    interior = sample.weights * (distance < radius)
    edge = sample.weights * (distance >= radius) * (distance < 2.25 * radius)
    return sample, interior, edge


def spot_reading(image, settings, correction):
    data = obj(settings, "斑点评估")
    center = coordinate(data.get("center"), "斑点中心")
    cx, cy = center
    if not 0 <= cx <= image.width or not 0 <= cy <= image.height:
        raise InputError("斑点中心超出照片")
    radius = number(data.get("radius", 12), "斑点取样半径", low=2, high=160)
    wide = 2.25 * radius
    limits = [max(0, cx - wide), max(0, cy - wide), min(image.width, cx + wide), min(image.height, cy + wide)]
    sample, inside, outer = annular_sample(image, limits, center=center, radius=radius)
    total_inner = float(inside.sum())
    total_outer = float(outer.sum())
    if total_inner < 6 or total_outer < 9:
        raise InputError("斑点和周边需要分别有足够的图像实底")
    lab = cielab(correction.apply(linearize(sample.rgb)))
    core = weighted_means(lab, inside)
    border = weighted_means(lab, outer)
    spread = weighted_means(delta_e(lab, border), outer)
    crop, info = crop_region(image, limits)
    canvas = crop.convert("RGBA")
    layer = Image.new("RGBA", canvas.size)
    draw = ImageDraw.Draw(layer)
    ox, oy = math.floor(limits[0]), math.floor(limits[1])
    sx, sy = cx - ox, cy - oy
    draw.ellipse((sx - radius, sy - radius, sx + radius, sy + radius), outline="#ffe860", width=2)
    draw.ellipse((sx - wide, sy - wide, sx + wide, sy + wide), outline="#60daec", width=2)
    canvas.alpha_composite(layer)
    return {
        "center": list(center), "radius_px": radius,
        "core_lab": [round(float(x), 4) for x in core],
        "surrounding_lab": [round(float(x), 4) for x in border],
        "center_delta_e": round(float(delta_e(core, border)), 5),
        "surrounding_variation": round(float(spread), 5),
        "core_coverage_px": round(total_inner, 2),
        "surrounding_coverage_px": round(total_outer, 2),
        "view": info,
    }, canvas


def grades(image, settings, correction):
    data = obj(settings, "评级")
    regions = seq(data.get("regions"), "评级样区", minimum=1, maximum=12)
    targets = {}
    for item in seq(data.get("targets"), "等级参考", minimum=2, maximum=5):
        raw = obj(item, "等级参考")
        name = text(raw.get("name"), "等级名", limit=25)
        if name in targets:
            raise InputError("等级名称不能重复")
        region = Region.read(raw.get("region"), *image.size).as_json()
        section = window(image, region, max_pixels=600000).require_pixels(8)
        targets[name] = weighted_means(cielab(correction.apply(linearize(section.rgb))), section.weights)
    outcome = []
    for item in regions:
        entry = obj(item, "评级样区")
        name = text(entry.get("name"), "样区名称", limit=35)
        area = Region.read(entry.get("region"), *image.size).as_json()
        sample = window(image, area, max_pixels=600000).require_pixels(8)
        average = weighted_means(cielab(correction.apply(linearize(sample.rgb))), sample.weights)
        ranking = sorted((float(delta_e(average, lab)), grade) for grade, lab in targets.items())
        limit = number(data.get("max_delta", 14), "最大接受色差", low=1, high=50)
        gap = number(data.get("min_gap", 1), "最小分辨间距", low=0, high=25)
        qualified = ranking[0][0] <= limit and ranking[1][0] - ranking[0][0] >= gap
        outcome.append({
            "name": name, "region": area, "grade": ranking[0][1] if qualified else None,
            "comparisons": [{"grade": label, "delta_e": round(distance, 4)} for distance, label in ranking],
            "coverage_px": round(sample.coverage, 4), "requires_review": not qualified,
        })
    distinct([result["name"] for result in outcome], "评级样区")
    return {"samples": outcome, "calibrated": bool(correction.samples)}
