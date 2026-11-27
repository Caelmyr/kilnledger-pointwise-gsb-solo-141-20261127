"""Exact overlap areas between grid cells, specimen regions and audit frames.

A grid covers a rectangular photographed region. Cells are rectangles in
photo coordinates. Specimen regions and archive audit frames can be rectangles
or simple polygons. Overlap area is computed by clipping polygons with the
Sutherland-Hodgman algorithm and taking the shoelace area, so the guaranteed
phase area never depends on pixel sampling.
"""

from dataclasses import dataclass

from .geometry import Box, polygon as read_polygon, signed_area, vertex_bounds
from .inputs import InputError, number, obj, seq

def _clip(subject, clip):
    """Clip a convex or simple polygon against a convex clipping polygon."""
    vertices = list(subject)
    for ax, ay, bx, by in _edges(clip):
        if not vertices:
            return []
        output = []
        previous = vertices[-1]
        previous_inside = _inside(previous, ax, ay, bx, by)
        for current in vertices:
            inside = _inside(current, ax, ay, bx, by)
            if inside != previous_inside:
                output.append(_intersection(previous, current, ax, ay, bx, by))
            if inside:
                output.append(current)
            previous, previous_inside = current, inside
        vertices = output
    return vertices


def _edges(polygon):
    points = list(polygon)
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        yield start[0], start[1], end[0], end[1]


def _inside(point, ax, ay, bx, by):
    # CCW clipping polygon: the interior is on the left of each directed edge.
    return (bx - ax) * (point[1] - ay) - (by - ay) * (point[0] - ax) >= -1e-9


def _intersection(previous, current, ax, ay, bx, by):
    px, py = previous
    qx, qy = current
    dcx, dcy = bx - ax, by - ay
    dpx, dpy = qx - px, qy - py
    denominator = dcx * dpy - dcy * dpx
    if abs(denominator) <= 1e-12:
        return current
    # Parameter along the subject segment S + u (E - S).
    u = (dcx * (ay - py) - dcy * (ax - px)) / denominator
    return px + u * dpx, py + u * dpy


def polygon_area(vertices):
    if len(vertices) < 3:
        return 0.0
    return abs(signed_area(vertices))


def overlap_area(first, second):
    return polygon_area(_clip(list(first), _convex(second)))


def _convex(vertices):
    points = list(vertices)
    if signed_area(points) < 0:
        points.reverse()
    return points


@dataclass(frozen=True)
class GridGeometry:
    rows: int
    columns: int
    bounds: Box

    @property
    def cell_width(self):
        return self.bounds.width / self.columns

    @property
    def cell_height(self):
        return self.bounds.height / self.rows

    def cell_box(self, row, column):
        left = self.bounds.left + column * self.cell_width
        top = self.bounds.top + row * self.cell_height
        return Box(left, top, left + self.cell_width, top + self.cell_height)

    def cell_polygon(self, row, column):
        return self.cell_box(row, column).vertices()

    def overlap(self, row, column, polygon):
        return overlap_area(self.cell_polygon(row, column), polygon)


def frame_polygon(raw, width, height, field="留档核框"):
    """Read an audit frame as photo-coordinate vertices."""
    if raw is None:
        return None
    if isinstance(raw, list):
        return Box.read(raw, field=field).vertices()
    data = obj(raw, field)
    if data.get("points") is not None:
        vertices = read_polygon(data["points"], field, max_points=32)
        if not vertex_bounds(vertices).inside(width, height):
            raise InputError(f"{field}超出原图")
        return vertices
    if data.get("box") is not None:
        box = Box.read(data["box"], field=field)
        if not box.inside(width, height):
            raise InputError(f"{field}超出原图")
        return box.vertices()
    raise InputError(f"{field}需要框选或多边形顶点")


def region_polygon(region):
    if region.vertices is not None:
        return region.vertices
    return region.bounds.vertices()


@dataclass(frozen=True)
class AreaGate:
    name: str
    phase: str
    minimum_fraction: float
    polygon: tuple
    region_area: float

    @classmethod
    def read(cls, raw, known_phases, width, height):
        data = obj(raw, "面积放行条件")
        name = str(data.get("name") or "面积条件")
        phase = data.get("phase")
        if phase not in known_phases:
            raise InputError(f"「{name}」指定的釉相不在参考色中")
        minimum = number(data.get("minimum_fraction"), f"「{name}」最低面积占比", low=0.0, high=1.0)
        vertices = frame_polygon(data.get("frame"), width, height, f"「{name}」核框")
        if vertices is None:
            raise InputError(f"「{name}」需要框一块核对面")
        area = polygon_area(vertices)
        if area <= 0:
            raise InputError(f"「{name}」核框没有有效面积")
        return cls(name, phase, float(minimum), tuple(vertices), area)


def gate_weights(geometry, gate, solid_cells):
    return {cell: geometry.overlap(cell[0], cell[1], gate.polygon) for cell in solid_cells}


def specimen_requirements(raw, geometry, known_phases, solid_cells, width, height):
    """Read glaze-record area lower bounds over specimen sample regions."""
    requirements = []
    for index, item in enumerate(seq(raw, "涂釉样区面积", maximum=12)):
        data = obj(item, "涂釉样区面积")
        name = str(data.get("name") or f"样区{index + 1}")
        phase = data.get("phase")
        if phase not in known_phases:
            raise InputError(f"「{name}」指定的釉相不在参考色中")
        minimum = number(data.get("minimum_fraction"), f"「{name}」最低面积占比", low=0.0, high=1.0)
        polygon = frame_polygon(data.get("frame"), width, height, f"「{name}」样区")
        if polygon is None:
            raise InputError(f"「{name}」需要框出涂釉样区")
        area = polygon_area(polygon)
        weights = {cell: geometry.overlap(cell[0], cell[1], polygon) for cell in solid_cells}
        requirements.append({
            "name": name, "phase": phase, "minimum_fraction": float(minimum),
            "polygon": tuple(polygon), "area": area, "weights": weights,
        })
    return requirements
