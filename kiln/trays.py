"""Transcribe visible coupon identities on one photographed tray."""

from dataclasses import dataclass, replace
import math

import numpy as np
from PIL import Image, ImageDraw

from .geometry import Affine, vertex_bounds
from .imaging import flatten, rectified, window
from .inputs import Conflict, InputError, choice, coordinate, distinct, flag, integer, obj, optional_text, seq, text


STATES = ("unread", "identified", "empty", "covered")
EDGE = 36
MAX_CELLS = 96


def cell_code(row, column):
    return f"{chr(65 + row)}{column + 1}"


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    state: str = "unread"
    coupon: str = ""
    note: str = ""

    @classmethod
    def read(cls, raw, rows, columns):
        value = obj(raw, "托盘格")
        row = integer(value.get("row"), "行", high=rows - 1)
        column = integer(value.get("column"), "列", high=columns - 1)
        state = choice(value.get("state", "unread"), "格子状态", STATES)
        coupon = optional_text(value.get("coupon"), "实物编号", 30)
        note = optional_text(value.get("note"), "抄录备注", 120)
        if state == "identified" and not coupon:
            raise InputError(f"{cell_code(row, column)}需要实物编号")
        if state != "identified" and coupon:
            raise InputError(f"{cell_code(row, column)}未确认，不能留下已确认的编号")
        return cls(row, column, state, coupon, note)

    @property
    def address(self):
        return cell_code(self.row, self.column)

    def as_json(self):
        return {
            "row": self.row, "column": self.column, "address": self.address,
            "state": self.state, "coupon": self.coupon, "note": self.note,
        }


@dataclass(frozen=True)
class Tray:
    name: str
    rows: int
    columns: int
    origin: tuple
    last_column: tuple
    last_row: tuple
    cells: tuple
    note: str
    backlit: bool

    @classmethod
    def read(cls, raw, width, height):
        user = obj(raw, "托盘")
        name = text(user.get("name"), "托盘名", limit=65)
        rows = integer(user.get("rows"), "行数", low=1, high=12)
        columns = integer(user.get("columns"), "列数", low=1, high=12)
        if rows * columns > MAX_CELLS:
            raise InputError(f"托盘最多 {MAX_CELLS} 格")
        origin = coordinate(user.get("origin"), "左上角")
        last_column = coordinate(user.get("last_column"), "右上角")
        last_row = coordinate(user.get("last_row"), "左下角")
        note = optional_text(user.get("note"), "托盘备注", 280)
        backlit = flag(user.get("backlit", False), "背光照")
        transform = Affine.axes(origin, last_column, last_row)
        if math.dist(origin, last_column) / columns < 5 or math.dist(origin, last_row) / rows < 5:
            raise InputError("每格沿两轴均需要至少五个照片像素")
        opposite = transform.point(1, 1)
        for x, y in (origin, last_column, last_row, opposite):
            if x < 0 or y < 0 or x > width or y > height:
                raise InputError("托盘仿射框超出照片")
        explicit = [Cell.read(entry, rows, columns)
                    for entry in seq(user.get("cells", []), "托盘格", maximum=MAX_CELLS)]
        distinct([cell.address for cell in explicit], "托盘格")
        identifiers = [cell.coupon.casefold() for cell in explicit if cell.coupon]
        if len(set(identifiers)) != len(identifiers):
            raise InputError("一张托盘照片中，一个实物编号只可出现一次")
        saved = {(cell.row, cell.column): cell for cell in explicit}
        cells = tuple(saved.get((row, column), Cell(row, column))
                      for row in range(rows) for column in range(columns))
        return cls(name, rows, columns, origin, last_column, last_row, cells, note, backlit)

    @property
    def transform(self):
        return Affine.axes(self.origin, self.last_column, self.last_row)

    def cell(self, row, column):
        if 0 <= row < self.rows and 0 <= column < self.columns:
            return self.cells[row * self.columns + column]
        raise InputError("所选格不在托盘内")

    def locate(self, point):
        x, y = self.transform.inverse(*point)
        if not (0 <= x < 1 and 0 <= y < 1):
            return None
        return self.cell(int(y * self.rows), int(x * self.columns))

    def polygon(self, cell, inset=0):
        x0 = (cell.column + inset) / self.columns
        y0 = (cell.row + inset) / self.rows
        x1 = (cell.column + 1 - inset) / self.columns
        y1 = (cell.row + 1 - inset) / self.rows
        return [list(self.transform.point(x, y)) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]

    def cell_visual(self, image, cell, size=144, inset=0.045):
        x0 = (cell.column + inset) / self.columns
        y0 = (cell.row + inset) / self.rows
        x1 = (cell.column + 1 - inset) / self.columns
        y1 = (cell.row + 1 - inset) / self.rows
        horizontal = math.hypot(self.transform.ux, self.transform.uy) / self.columns
        vertical = math.hypot(self.transform.vx, self.transform.vy) / self.rows
        width = max(16, round(size * horizontal / max(horizontal, vertical)))
        height = max(16, round(size * vertical / max(horizontal, vertical)))
        return rectified(image, self.transform, x0, y0, x1, y1, width, height)

    def with_cell(self, row, column, raw):
        self.cell(row, column)
        values = {**obj(raw, "托盘格"), "row": row, "column": column}
        updated = Cell.read(values, self.rows, self.columns)
        if updated.coupon and any(
            other.address != updated.address and other.coupon.casefold() == updated.coupon.casefold()
            for other in self.cells
        ):
            raise Conflict("托盘别处已有同编号实物")
        index = row * self.columns + column
        return replace(self, cells=self.cells[:index] + (updated,) + self.cells[index + 1:])

    def summaries(self):
        counts = {key: 0 for key in STATES}
        for cell in self.cells:
            counts[cell.state] += 1
        return counts

    def to_json(self):
        return {
            "name": self.name, "rows": self.rows, "columns": self.columns,
            "origin": list(self.origin), "last_column": list(self.last_column), "last_row": list(self.last_row),
            "cells": [cell.as_json() for cell in self.cells],
            "note": self.note, "backlit": self.backlit, "counts": self.summaries(),
        }


def cell_quality(image, tray, cell):
    polygon = tray.polygon(cell, inset=0.08)
    sample = window(image, {"points": polygon}, max_pixels=1_400_000)
    if sample.coverage < 2:
        return {"coverage": 0, "sharpness": None, "photo_bounds": vertex_bounds(tuple(map(tuple, polygon))).as_list()}
    rgb = sample.rgb.astype(np.float32).mean(axis=2) / 255
    dx = np.abs(np.diff(rgb, axis=1))
    dy = np.abs(np.diff(rgb, axis=0))
    wx = np.minimum(sample.weights[:, 1:], sample.weights[:, :-1])
    wy = np.minimum(sample.weights[1:, :], sample.weights[:-1, :])
    total = wx.sum() + wy.sum()
    sharpness = (float((dx * wx).sum() + (dy * wy).sum()) / total) if total else 0
    return {
        "coverage": round(sample.coverage / sample.geometric_area, 4),
        "sharpness": round(float(sharpness), 5),
        "photo_bounds": vertex_bounds(tuple(map(tuple, polygon))).as_list(),
    }


def grid_svg(tray, width, height):
    groups = []
    colors = {"identified": "#7adca6", "unread": "#f7cc5c", "covered": "#fa9470", "empty": "#b9c3c9"}
    for cell in tray.cells:
        polygon = tray.polygon(cell)
        points = " ".join(f"{x:.3f},{y:.3f}" for x, y in polygon)
        color = colors[cell.state]
        x = sum(vertex[0] for vertex in polygon) / 4
        y = sum(vertex[1] for vertex in polygon) / 4
        groups.append(f'<polygon points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        groups.append(f'<text x="{x:.3f}" y="{y:.3f}" fill="{color}" text-anchor="middle" '
                      f'font-size="16" font-family="sans-serif">{cell.address}</text>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
            + "".join(groups) + "</svg>").encode("utf-8")


def contact_sheet(image, tray):
    cell_width = 152
    cell_height = 134
    if tray.columns > 8:
        cell_width = 114
    output = Image.new("RGB", (tray.columns * cell_width + EDGE * 2, tray.rows * cell_height + EDGE * 2 + 18), "#ede7da")
    draw = ImageDraw.Draw(output)
    draw.text((EDGE, 16), f"TRAY CONTACT  /  {tray.rows} x {tray.columns}", fill="#504836")
    for cell in tray.cells:
        left = EDGE + cell.column * cell_width
        top = EDGE + cell.row * cell_height
        inset = 5
        tile = tray.cell_visual(image, cell, size=min(cell_width - 12, cell_height - 31))
        if cell.state == "empty":
            tile = Image.new("RGBA", tile.size, "#ddd6c7")
        picture_y = top + 3 + (cell_height - 30 - tile.height) // 2
        output.paste(flatten(tile, (226, 216, 197)), (left + (cell_width - tile.width) // 2, picture_y))
        draw.rectangle((left, top, left + cell_width - inset, top + cell_height - inset), outline="#c4b799")
        tag = f"{cell.address} / {cell.state.upper()}"
        draw.text((left + 7, top + cell_height - 27), tag, fill="#323022")
    return output
