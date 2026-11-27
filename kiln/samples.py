"""Reusable photo selections with independent source coordinates."""

from dataclasses import dataclass
import math

from PIL import ImageDraw

from .geometry import Region
from .imaging import flatten, thumbnail, window
from .inputs import InputError, choice, coordinate, distinct, obj, optional_text, seq, text


LABEL_TYPES = ("routine", "control", "attention")


@dataclass(frozen=True)
class Selection:
    label: str
    category: str
    kind: str
    region: Region | None
    points: tuple
    note: str

    @classmethod
    def read(cls, user, width, height):
        data = obj(user, "影像位置")
        title = text(data.get("name"), "位置名称", limit=65)
        category = choice(data.get("category", "routine"), "位置用途", LABEL_TYPES)
        note = optional_text(data.get("note"), "检视札记", 300)
        field = choice(data.get("shape", "region"), "位置形状", ("region", "points"))
        if field == "region":
            if data.get("region") is None:
                raise InputError("先框选需要保留的位置")
            region = Region.read(data["region"], width, height)
            points = ()
        else:
            region = None
            points = tuple(coordinate(raw, "标记坐标") for raw in seq(data.get("points"), "标记坐标", minimum=1, maximum=32))
            distinct(points, "标记坐标")
            for x, y in points:
                if not 0 <= x <= width or not 0 <= y <= height:
                    raise InputError("要保留的标记超出照片")
        return cls(title, category, field, region, points, note)

    @property
    def base(self):
        return {
            "name": self.label, "category": self.category, "shape": self.kind, "note": self.note,
            "region": self.region.as_json() if self.region is not None else None,
            "points": [list(point) for point in self.points],
        }

    def measure(self, image, ruler):
        if self.region:
            active = window(image, self.region.as_json())
            measurements = {
                "origin": [active.left, active.top], "size": [active.width, active.height],
                "solid_px": round(active.coverage, 4),
                "solid_fraction": round(active.coverage / active.geometric_area, 5) if active.geometric_area else 0,
            }
            if ruler:
                measurements["physical_area_mm2"] = round(active.coverage * ruler.per_pixel ** 2, 4)
        else:
            travel = math.fsum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))
            measurements = {"node_count": len(self.points), "length_px": round(travel, 4)}
            if ruler:
                measurements["length_mm"] = round(travel * ruler.per_pixel, 4)
        return measurements

    def preview(self, original):
        view = thumbnail(original, 1080)
        background = flatten(view, (227, 215, 198))
        horizontal, vertical = view.width / original.width, view.height / original.height
        ink = {"routine": "#edb66e", "control": "#77cdb8", "attention": "#ef827f"}[self.category]
        draw = ImageDraw.Draw(background)
        if self.region:
            outline = self.region.vertices or self.region.bounds.vertices()
            display = [(x * horizontal, y * vertical) for x, y in outline]
            draw.line(display + display[:1], fill=ink, width=3)
        else:
            display = [(x * horizontal, y * vertical) for x, y in self.points]
            if len(display) > 1:
                draw.line(display, fill=ink, width=2)
            for i, (x, y) in enumerate(display, 1):
                draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=ink, outline="#292319")
                draw.text((x + 7, y - 5), str(i), fill=ink, stroke_width=1, stroke_fill="#2a211a")
        return background


def make(photo, image, raw, ruler, reference=None):
    selection = Selection.read(raw, *image.size)
    if reference is not None:
        source = {"id": reference["id"], "revision": reference["revision"]}
    else:
        source = None
    payload = {
        **selection.base, "measurement": selection.measure(image, ruler),
        "source_photo_sha256": photo["png_sha"], "ruler": source,
        "source_photo_id": photo["id"], "format": "png",
    }
    return payload, selection.preview(image)


def select_for_tool(saved, active, settings, kind):
    if saved["kind"] != "selection" or saved["photo_id"] != active["id"]:
        raise InputError("选择的位置未保存在当前原照")
    payload = saved["payload"]
    if payload["source_photo_sha256"] != active["png_sha"]:
        raise InputError("保留的位置对应的原照片内容已改变")
    if payload["shape"] == "region":
        direction = ("line", "ruler", "traverse", "spot", "rectify", "layer")
        if kind in direction:
            raise InputError("此工具需要点或折线，位置档案是区域")
        return {**settings, "region": payload["region"]}
    points = payload["points"]
    if kind in ("line", "ruler"):
        if len(points) != 2:
            raise InputError("样区档案需要正好两个点")
        return {**settings, "start": points[0], "end": points[1]}
    if kind in ("rectify", "traverse"):
        return {**settings, "points": points}
    if kind == "spot":
        return {**settings, "center": points[0]}
    raise InputError("此工具需要区域或独立设置，位置档案是单点或折线")
