"""Measure a glaze band between visible cross-section interfaces."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import ImageDraw

from .calibration import cielab, encode, html_color, linearize, weighted_means
from .geometry import Affine
from .imaging import flatten, image_samples, thumbnail
from .inputs import InputError, coordinate, integer, number, obj, seq


@dataclass(frozen=True)
class Section:
    origin: tuple
    tangent: tuple
    inward: tuple
    outer: tuple
    inner: tuple
    basis: Affine

    @classmethod
    def read(cls, data, width, height):
        origin = coordinate(data.get("origin"), "剖面起点")
        tangent = coordinate(data.get("tangent"), "沿界面方向点")
        inward = coordinate(data.get("inward"), "向内部方向点")
        along = np.asarray(tangent, dtype=np.float64) - np.asarray(origin)
        across = np.asarray(inward) - np.asarray(origin)
        if np.linalg.norm(along) < 5 or np.linalg.norm(across) < 2:
            raise InputError("剖面方向的参考点过近")
        along /= np.linalg.norm(along)
        normal = np.array([-along[1], along[0]])
        if abs(float(across @ normal)) < 1.5:
            raise InputError("界面方向点和内向点需要明显分离")
        if across @ normal < 0:
            normal *= -1
        basis = Affine.axes(origin, np.asarray(origin) + along, np.asarray(origin) + normal)
        traces = []
        for name, prop in (("外釉界", "outer"), ("内坯界", "inner")):
            raw = seq(data.get(prop), name, minimum=2, maximum=24)
            points = [coordinate(coords, name) for coords in raw]
            if any(not 0 <= a <= width or not 0 <= b <= height for a, b in points):
                raise InputError(f"{name}超出照片")
            local = sorted(basis.inverse(*point) for point in points)
            if any(right[0] - left[0] < 0.25 for left, right in zip(local, local[1:])):
                raise InputError(f"{name}的两个控制点在沿界面方向重叠")
            traces.append(tuple(local))
        return cls(origin, tangent, inward, *traces, basis)

    def stations(self, spacing):
        left = max(self.outer[0][0], self.inner[0][0])
        right = min(self.outer[-1][0], self.inner[-1][0])
        if right - left < max(3, spacing):
            raise InputError("两条界面几乎没有共同测量跨度")
        count = math.ceil((right - left) / spacing) + 1
        if count > 512:
            raise InputError("剖面站点过多，请增大采样间距")
        sampling = np.linspace(left, right, count)
        outer = np.interp(sampling, *np.array(self.outer).T)
        inner = np.interp(sampling, *np.array(self.inner).T)
        if np.any(inner <= outer):
            raise InputError("两条界面发生交叉，或内外方向未选对")
        # Crossings can occur at entered knots even when between rendered stations.
        knots = np.array(sorted({u for path in (self.outer, self.inner) for u, _ in path if left <= u <= right}))
        if knots.size and np.any(np.interp(knots, *np.array(self.inner).T) <= np.interp(knots, *np.array(self.outer).T)):
            raise InputError("两条界面在标注控制点之间发生交叉")
        return sampling, outer, inner


def profiles(image, section, stations, outer, inner, settings, correction, ruler):
    samples = integer(settings.get("across_samples", 16), "每站釉带采样数", low=6, high=64)
    required = number(settings.get("minimum_coverage", 0.7), "站点最小实底", low=0.15, high=1)
    frac = (np.arange(samples, dtype=np.float64)[None, :] + 0.5) / samples
    depth = outer[:, None] + frac * (inner - outer)[:, None]
    px, py = section.basis.point(stations[:, None], depth)
    rgb, supports = image_samples(image, px, py)
    corrected = correction.apply(linearize(rgb * 255))
    result = []
    for ix, x in enumerate(stations):
        coverage = float(supports[ix].mean())
        eligible = coverage >= required
        a, b = section.basis.point(x, outer[ix]), section.basis.point(x, inner[ix])
        width = float(inner[ix] - outer[ix])
        entry = {
            "station": ix + 1, "distance_px": round(float(x - stations[0]), 5),
            "band_px": round(width, 5), "coverage": round(coverage, 5),
            "outer": [round(float(v), 4) for v in a], "inner": [round(float(v), 4) for v in b],
            "accepted": eligible,
        }
        if ruler is not None:
            entry["band_mm"] = round(width * ruler.per_pixel, 5)
        if eligible:
            observed = weighted_means(corrected[ix], supports[ix])
            entry["color"] = html_color(encode(observed))
            entry["lab"] = [round(float(c), 4) for c in cielab(observed)]
        else:
            entry["color"] = None
            entry["lab"] = None
        result.append(entry)
    return result


def section_overlay(image, lines):
    thumb = flatten(thumbnail(image, 1100), (246, 234, 218))
    horizontal = thumb.width / image.width
    vertical = thumb.height / image.height
    draw = ImageDraw.Draw(thumb)
    ends = [[], []]
    for line in lines:
        first, last = line["outer"], line["inner"]
        start = (first[0] * horizontal, first[1] * vertical)
        end = (last[0] * horizontal, last[1] * vertical)
        ends[0].append(start)
        ends[1].append(end)
        tint = "#fbd57d" if line["accepted"] else "#f49e7e"
        if len(lines) < 55 or line["station"] % math.ceil(len(lines) / 55) == 0:
            draw.line((*start, *end), fill=tint, width=2)
    for vertices, color in zip(ends, ("#72cabb", "#d09bda")):
        if len(vertices) >= 2:
            draw.line(vertices, fill=color, width=3)
    return thumb


def summary(lines, ruler):
    accepted = [line for line in lines if line["accepted"]]
    measurements = [line["band_px"] for line in accepted]
    if not measurements:
        return {"accepted_stations": 0, "thickness_px": None, "thickness_mm": None}
    edges = np.quantile(measurements, [0.1, 0.5, 0.9])
    thickness_px = [round(float(value), 5) for value in edges]
    return {
        "accepted_stations": len(accepted),
        "thickness_px": thickness_px,
        "thickness_mm": [round(float(value) * ruler.per_pixel, 5) for value in edges] if ruler else None,
        "measurement_order": ["p10", "p50", "p90"],
    }


def examine(image, settings, correction, ruler):
    data = obj(settings, "剖面厚度")
    section = Section.read(data, *image.size)
    spacing = number(data.get("spacing", 6), "沿界面采样距离", low=0.75, high=50)
    positions, outer, inner = section.stations(spacing)
    values = profiles(image, section, positions, outer, inner, data, correction, ruler)
    return {
        **summary(values, ruler), "station_count": len(values), "stations": values,
        "basis": section.basis.as_json(),
        "span_px": round(float(positions[-1] - positions[0]), 5),
    }, section_overlay(image, values)
