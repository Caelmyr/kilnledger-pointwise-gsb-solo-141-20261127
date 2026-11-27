"""Rectify known planar specimen outlines and report print scale."""

from dataclasses import dataclass
import math

import numpy as np
from PIL import Image, ImageDraw

from .geometry import center_mesh, orient, polygon, signed_area
from .imaging import MAX_OPERATION_PIXELS, sampled_image
from .inputs import InputError, choice, flag, number, obj


EPS = 1e-9


@dataclass(frozen=True)
class Panel:
    points: tuple
    width_mm: float
    height_mm: float
    resolution: float
    overhang: float
    mirror: bool
    canvas: str

    @classmethod
    def read(cls, raw, image_size):
        data = obj(raw, "试片校直")
        points = polygon(data.get("points"), "左上顺时针四角", max_points=4)
        if len(points) != 4:
            raise InputError("校直使用四个角")
        normalized = [orient(points[i], points[(i + 1) % 4], points[(i + 2) % 4]) for i in range(4)]
        if min(normalized) <= EPS or signed_area(points) < 24:
            raise InputError("请以左上、右上、右下、左下的顺序取凸四边形")
        for x, y in points:
            if not 0 <= x <= image_size[0] or not 0 <= y <= image_size[1]:
                raise InputError("四个参考角必须在照片上")
        if min(math.dist(a, b) for a, b in zip(points, points[1:] + points[:1])) < 6:
            raise InputError("相邻角需要相距至少六个像素")
        width = number(data.get("width_mm"), "实物宽度", low=1, high=800)
        height = number(data.get("height_mm"), "实物高度", low=1, high=800)
        resolution = number(data.get("pixels_per_mm", 7), "每毫米像素数", low=1, high=22)
        overhang = number(data.get("padding_mm", 0), "环境余量", low=0, high=20)
        canvas = choice(data.get("canvas", "transparent"), "校直背景", ("transparent", "cream", "black"))
        mirror = flag(data.get("mirror", False), "以背面观察")
        return cls(points, width, height, resolution, overhang, mirror, canvas)

    @property
    def raster_size(self):
        width = round((self.width_mm + 2 * self.overhang) * self.resolution)
        height = round((self.height_mm + 2 * self.overhang) * self.resolution)
        if min(width, height) < 3 or width * height > MAX_OPERATION_PIXELS:
            raise InputError("该尺寸和分辨率超出校直输出范围")
        return width, height

    def target_grid(self):
        width, height = self.raster_size
        x, y = center_mesh(0, 0, width, height)
        x = x * (self.width_mm + 2 * self.overhang) / width - self.overhang
        y = y * (self.height_mm + 2 * self.overhang) / height - self.overhang
        x = x / self.width_mm
        y = y / self.height_mm
        if self.mirror:
            x = 1 - x
        return x, y


def fit_projective(points):
    desired = ((0, 0), (1, 0), (1, 1), (0, 1))
    origin = tuple(np.mean(points, axis=0))
    radius = max(math.dist(origin, point) for point in points)
    design = []
    target = []
    for (u, v), (x, y) in zip(desired, points):
        x = (x - origin[0]) / radius
        y = (y - origin[1]) / radius
        design.append((u, v, 1, 0, 0, 0, -u * x, -v * x))
        design.append((0, 0, 0, u, v, 1, -u * y, -v * y))
        target.extend((x, y))
    if np.linalg.cond(design) > 10_000:
        raise InputError("选定四个角导致转换不稳定")
    try:
        coefficients = np.linalg.solve(design, target)
    except np.linalg.LinAlgError:
        raise InputError("这四个角不能校直") from None
    a, b, c, d, e, f, g, h = coefficients
    return np.array([
        [a * radius + origin[0] * g, b * radius + origin[0] * h, c * radius + origin[0]],
        [d * radius + origin[1] * g, e * radius + origin[1] * h, f * radius + origin[1]],
        [g, h, 1],
    ])


def source_coords(matrix, x, y):
    denominator = matrix[2, 0] * x + matrix[2, 1] * y + 1
    if float(np.min(denominator)) < 0.035:
        raise InputError("在给定环境余量上无法保持转换的连续性")
    return (
        (matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2]) / denominator,
        (matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2]) / denominator,
    )


def jacobian(matrix, x, y):
    point_x, point_y = source_coords(matrix, x, y)
    denominator = matrix[2, 0] * x + matrix[2, 1] * y + 1
    return np.array([
        [(matrix[0, 0] - point_x * matrix[2, 0]) / denominator,
         (matrix[0, 1] - point_x * matrix[2, 1]) / denominator],
        [(matrix[1, 0] - point_y * matrix[2, 0]) / denominator,
         (matrix[1, 1] - point_y * matrix[2, 1]) / denominator],
    ])


def resampling_quality(matrix, panel):
    scales = []
    for y in (0, 0.5, 1):
        for x in (0, 0.5, 1):
            local = jacobian(matrix, x, y)
            local[:, 0] /= panel.width_mm
            local[:, 1] /= panel.height_mm
            stretches = np.linalg.svd(local, compute_uv=False)
            scales.append({
                "normalized": [x, y],
                "source_px_per_mm_min": round(float(stretches[1]), 5),
                "source_px_per_mm_max": round(float(stretches[0]), 5),
                "output_upsamples": bool(stretches[1] < panel.resolution),
            })
    return scales


def backing(image, palette):
    if palette == "transparent":
        return image
    base = Image.new("RGBA", image.size, "#ede0c8" if palette == "cream" else "#1c1d1c")
    base.alpha_composite(image)
    return base.convert("RGB")


def draw_grid(image, settings, panel):
    data = obj(settings, "网尺")
    interval = number(data.get("interval", 10), "网尺间距（毫米）", low=1, high=100)
    clip = flag(data.get("in_specimen", True), "网线限于实物")
    color = choice(data.get("ink", "dark"), "标尺线色", ("dark", "light"))
    line_color = "#f5e9d38c" if color == "light" else "#25180e82"
    step_x = image.width / (panel.width_mm + 2 * panel.overhang)
    step_y = image.height / (panel.height_mm + 2 * panel.overhang)
    x0, y0 = panel.overhang * step_x, panel.overhang * step_y
    x1, y1 = x0 + panel.width_mm * step_x, y0 + panel.height_mm * step_y
    layer = Image.new("RGBA", image.size)
    draw = ImageDraw.Draw(layer)
    for i in range(math.floor(panel.width_mm / interval) + 1):
        x = x0 + i * interval * step_x
        draw.line((x, y0 if clip else 0, x, y1 if clip else image.height), fill=line_color)
    for i in range(math.floor(panel.height_mm / interval) + 1):
        y = y0 + i * interval * step_y
        draw.line((x0 if clip else 0, y, x1 if clip else image.width, y), fill=line_color)
    result = image.convert("RGBA")
    result.alpha_composite(layer)
    return result


def straighten(image, settings):
    data = obj(settings, "校直")
    panel = Panel.read(data, image.size)
    matrix = fit_projective(panel.points)
    grid = panel.target_grid()
    lx, ly = source_coords(matrix, *grid)
    view = sampled_image(image, lx, ly)
    w, h = view.size
    x0 = math.ceil(panel.overhang / (panel.width_mm + 2 * panel.overhang) * w)
    y0 = math.ceil(panel.overhang / (panel.height_mm + 2 * panel.overhang) * h)
    core = np.asarray(view.crop((x0, y0, w - x0, h - y0)).getchannel("A"), dtype=np.float32)
    if not core.size or core.mean() < 8:
        raise InputError("校直区域内未保留可读釉面")
    output = backing(view, panel.canvas)
    if data.get("grid") is not None:
        output = draw_grid(output, data["grid"], panel)
    return {
        "size_mm": [panel.width_mm, panel.height_mm], "padding_mm": panel.overhang,
        "output_pixels": list(view.size), "requested_pixels_per_mm": panel.resolution,
        "visible_fraction_in_specimen": round(float(core.mean()) / 255, 6),
        "scale_grid": resampling_quality(matrix, panel),
        "source_corners": [list(point) for point in panel.points], "mirrored": panel.mirror,
        "source_matrix": np.round(matrix, 8).tolist(),
    }, output
