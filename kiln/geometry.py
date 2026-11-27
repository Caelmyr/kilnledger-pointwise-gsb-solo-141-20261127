"""Image-edge coordinates, regions and metric transforms."""

from dataclasses import dataclass
import math

import numpy as np

from .inputs import InputError, coordinate, number, obj, seq


EPSILON = 1e-8


@dataclass(frozen=True)
class Box:
    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def read(cls, raw, *, field="样区"):
        values = seq(raw, field, minimum=4, maximum=4)
        box = cls(*(number(v, field, low=-25000, high=25000) for v in values))
        if box.width < EPSILON or box.height < EPSILON:
            raise InputError(f"{field}必须有正的宽高", field=field)
        return box

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top

    def as_list(self):
        return [self.left, self.top, self.right, self.bottom]

    def inside(self, width, height):
        return self.left >= 0 and self.top >= 0 and self.right <= width and self.bottom <= height

    def expand(self, amount):
        return Box(self.left - amount, self.top - amount, self.right + amount, self.bottom + amount)

    def integer_extent(self, width, height):
        return (
            max(0, math.floor(self.left)),
            max(0, math.floor(self.top)),
            min(width, math.ceil(self.right)),
            min(height, math.ceil(self.bottom)),
        )

    def vertices(self):
        return (
            (self.left, self.top),
            (self.right, self.top),
            (self.right, self.bottom),
            (self.left, self.bottom),
        )


def center_mesh(left, top, width, height):
    yy, xx = np.ogrid[top:top + height, left:left + width]
    return xx.astype(np.float64) + 0.5, yy.astype(np.float64) + 0.5


def box_coverage(box, world_left, world_top, width, height):
    x = np.arange(world_left, world_left + width, dtype=np.float32)
    y = np.arange(world_top, world_top + height, dtype=np.float32)
    dx = np.clip(np.minimum(x + 1, box.right) - np.maximum(x, box.left), 0, 1)
    dy = np.clip(np.minimum(y + 1, box.bottom) - np.maximum(y, box.top), 0, 1)
    return dy[:, None] * dx[None, :]


def signed_area(vertices):
    shifted = vertices[1:] + vertices[:1]
    return sum(px * qy - py * qx for (px, py), (qx, qy) in zip(vertices, shifted)) / 2


def vertex_bounds(vertices):
    return Box(
        min(x for x, _ in vertices),
        min(y for _, y in vertices),
        max(x for x, _ in vertices),
        max(y for _, y in vertices),
    )


def orient(p, q, r):
    return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])


def on_segment(p, q, r):
    return (
        min(p[0], q[0]) - EPSILON <= r[0] <= max(p[0], q[0]) + EPSILON
        and min(p[1], q[1]) - EPSILON <= r[1] <= max(p[1], q[1]) + EPSILON
        and abs(orient(p, q, r)) <= EPSILON
    )


def segments_intersect(a, b, c, d):
    ab_c, ab_d = orient(a, b, c), orient(a, b, d)
    cd_a, cd_b = orient(c, d, a), orient(c, d, b)
    return (
        ((ab_c > EPSILON and ab_d < -EPSILON) or (ab_c < -EPSILON and ab_d > EPSILON))
        and ((cd_a > EPSILON and cd_b < -EPSILON) or (cd_a < -EPSILON and cd_b > EPSILON))
    ) or any((
        on_segment(a, b, c),
        on_segment(a, b, d),
        on_segment(c, d, a),
        on_segment(c, d, b),
    ))


def polygon(value, field="多边形", *, max_points=64):
    vertices = tuple(
        coordinate(p, f"{field}点{i + 1}")
        for i, p in enumerate(seq(value, field, minimum=3, maximum=max_points))
    )
    if len(set(vertices)) != len(vertices) or abs(signed_area(vertices)) <= EPSILON:
        raise InputError(f"{field}重复或没有有效面积", field=field)
    edges = list(zip(vertices, vertices[1:] + vertices[:1]))
    for i, (a, b) in enumerate(edges):
        for j in range(i + 2, len(edges)):
            if i == 0 and j == len(edges) - 1:
                continue
            c, d = edges[j]
            if segments_intersect(a, b, c, d):
                raise InputError(f"{field}的边互相穿过", field=field)
    return vertices


def polygon_hits(vertices, x, y):
    included = np.zeros(np.broadcast_shapes(x.shape, y.shape), dtype=bool)
    for (ax, ay), (bx, by) in zip(vertices, vertices[1:] + vertices[:1]):
        if abs(by - ay) <= EPSILON:
            continue
        crosses = (ay > y) != (by > y)
        crossing = ax + (bx - ax) * (y - ay) / (by - ay)
        included ^= crosses & (x < crossing)
    return included


def polygon_coverage(vertices, left, top, width, height, *, sample_axis=2):
    x, y = center_mesh(left, top, width, height)
    counts = np.zeros((height, width), dtype=np.float32)
    for vy in range(sample_axis):
        for vx in range(sample_axis):
            dx = (vx + 0.5) / sample_axis - 0.5
            dy = (vy + 0.5) / sample_axis - 0.5
            counts += polygon_hits(vertices, x + dx, y + dy)
    return counts / (sample_axis ** 2)


@dataclass(frozen=True)
class Region:
    bounds: Box
    vertices: tuple | None = None

    @classmethod
    def read(cls, value, width, height):
        if value is None:
            return cls(Box(0, 0, width, height))
        if isinstance(value, list):
            box = Box.read(value)
            if not box.inside(width, height):
                raise InputError("样区超出原图")
            return cls(box)
        raw = obj(value, "样区")
        vertices = polygon(raw.get("points"), "样区", max_points=32)
        bounds = vertex_bounds(vertices)
        if not bounds.inside(width, height):
            raise InputError("样区多边形超出原图")
        return cls(bounds, vertices)

    def as_json(self):
        if self.vertices is not None:
            return {"points": [list(v) for v in self.vertices]}
        return self.bounds.as_list()

    def coverage(self, left, top, width, height):
        if self.vertices is None:
            return box_coverage(self.bounds, left, top, width, height)
        return polygon_coverage(self.vertices, left, top, width, height)


@dataclass(frozen=True)
class Affine:
    ox: float
    oy: float
    ux: float
    uy: float
    vx: float
    vy: float

    @classmethod
    def axes(cls, origin, x_mark, y_mark):
        ox, oy = origin
        xx, xy = x_mark
        yx, yy = y_mark
        result = cls(ox, oy, xx - ox, xy - oy, yx - ox, yy - oy)
        axis_length = math.hypot(result.ux, result.uy) * math.hypot(result.vx, result.vy)
        if axis_length < EPSILON or abs(result.determinant) / axis_length < 0.05:
            raise InputError("两个方向的参照线需要彼此分开")
        return result

    @property
    def determinant(self):
        return self.ux * self.vy - self.uy * self.vx

    def point(self, x, y):
        return self.ox + self.ux * x + self.vx * y, self.oy + self.uy * x + self.vy * y

    def inverse(self, x, y):
        dx = x - self.ox
        dy = y - self.oy
        return (
            (self.vy * dx - self.vx * dy) / self.determinant,
            (self.ux * dy - self.uy * dx) / self.determinant,
        )

    def as_json(self):
        return [self.ox, self.oy, self.ux, self.uy, self.vx, self.vy]


@dataclass(frozen=True)
class Ruler:
    start: tuple
    end: tuple
    millimeters: float

    @classmethod
    def read(cls, value, width, height):
        raw = obj(value, "标尺")
        start = coordinate(raw.get("start"), "标尺起点")
        end = coordinate(raw.get("end"), "标尺终点")
        for x, y in (start, end):
            if not (0 <= x <= width and 0 <= y <= height):
                raise InputError("标尺端点超出原图")
        length = number(raw.get("millimeters"), "标尺毫米", low=0.01, high=10000)
        if math.dist(start, end) < 3:
            raise InputError("标尺端点至少相隔三个像素")
        return cls(start, end, length)

    @property
    def per_pixel(self):
        return self.millimeters / math.dist(self.start, self.end)

    def to_json(self):
        return {
            "start": list(self.start),
            "end": list(self.end),
            "millimeters": self.millimeters,
            "mm_per_pixel": self.per_pixel,
        }


def evenly_spaced(start, end, distance):
    length = math.dist(start, end)
    count = max(2, math.ceil(length / distance) + 1)
    if count > 2048:
        raise InputError("测线超过 2048 个采样点，请增大步长")
    return (
        np.linspace(start[0], end[0], count),
        np.linspace(start[1], end[1], count),
        np.linspace(0, length, count),
    )
