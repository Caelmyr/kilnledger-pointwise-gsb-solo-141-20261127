"""Build and inspect glaze test matrices with recorded specimen photos."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction

from PIL import Image, ImageDraw

from .calibration import analyze, delta_e
from .geometry import Region
from .imaging import crop_region, flatten, thumbnail
from .inputs import InputError, distinct, in_project, integer, key, obj, optional_text, seq, text
from .refire import (
    candidates_from_entry,
    plan_refire,
    recheck_weighing,
    refire_plan,
    recheck_weights,
    verify_weights,
    weigh_table,
)


MAX_CELLS = 36


def decimal(value, field, *, low="0", high="10000"):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise InputError(f"{field}需要输入有限小数")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise InputError(f"{field}不是小数") from None
    if not result.is_finite() or not Decimal(low) <= result <= Decimal(high):
        raise InputError(f"{field}需要介于 {low} 与 {high} 之间")
    if result != result.quantize(Decimal("0.0001")):
        raise InputError(f"{field}只支持小数点后最多四位")
    return result


@dataclass(frozen=True)
class Mix:
    name: str
    materials: tuple

    @classmethod
    def read(cls, raw, field):
        data = obj(raw, field)
        title = text(data.get("name"), f"{field}名称", limit=42)
        materials = []
        for item in seq(data.get("materials"), "原料", minimum=1, maximum=12):
            part = obj(item, "原料")
            name = text(part.get("name"), "原料名", limit=38)
            ratio = decimal(part.get("parts"), "原料份数", low="0.0001")
            materials.append((name, ratio))
        distinct([name.casefold() for name, _ in materials], "原料名")
        return cls(title, tuple(materials))

    def ratios(self):
        total = sum(Fraction(value) for _, value in self.materials)
        return tuple((name, Fraction(value) / total) for name, value in self.materials)

    def as_json(self):
        return {
            "name": self.name, "materials": [{"name": name, "parts": str(amount)} for name, amount in self.materials],
        }


@dataclass(frozen=True)
class TestGrid:
    row_steps: int
    column_steps: int
    row_limit: Fraction
    column_limit: Fraction
    base: Mix
    row_mix: Mix
    column_mix: Mix
    grams: Decimal
    precision: Decimal

    @classmethod
    def read(cls, data):
        base = Mix.read(data.get("base"), "基础釉")
        row = Mix.read(data.get("row_mix"), "纵向混合料")
        column = Mix.read(data.get("column_mix"), "横向混合料")
        names = [name for recipe in (base, row, column) for name, _ in recipe.materials]
        canonical = {}
        for label in names:
            previous = canonical.setdefault(label.casefold(), label)
            if previous != label:
                raise InputError(f"共同使用同一原料时，请统一名称 {previous}")
        row_steps = integer(data.get("rows", 4), "阶梯纵格数", low=2, high=6)
        column_steps = integer(data.get("columns", 4), "阶梯横格数", low=2, high=6)
        if row_steps * column_steps > MAX_CELLS:
            raise InputError("一个配方展板最多 36 格")
        row_max = decimal(data.get("row_limit", "40"), "纵向最高占比", low="0.01", high="100")
        column_max = decimal(data.get("column_limit", "40"), "横向最高占比", low="0.01", high="100")
        grams = decimal(data.get("grams", "20"), "每格配料克数", low="2", high="500")
        precision = decimal(data.get("precision", "0.1"), "电子秤精度", low="0.01", high="2")
        if grams % precision:
            raise InputError("每格克数需要是秤精度的整数倍")
        return cls(row_steps, column_steps, Fraction(row_max) / 100, Fraction(column_max) / 100,
                   base, row, column, grams, precision)

    def blend(self, row, column):
        y = self.row_limit * Fraction(row, self.row_steps - 1)
        x = self.column_limit * Fraction(column, self.column_steps - 1)
        base = 1 - x - y
        if base < 0:
            return None
        entries = {}
        for weight, recipe in zip((base, y, x), (self.base, self.row_mix, self.column_mix)):
            for name, ratio in recipe.ratios():
                entries[name] = entries.get(name, Fraction(0)) + ratio * weight
        return base, y, x, entries

    def round_weights(self, entries):
        units = int(self.grams / self.precision)
        exact = {name: units * share for name, share in entries.items()}
        rounded = {name: amount.numerator // amount.denominator for name, amount in exact.items()}
        shortfall = units - sum(rounded.values())
        order = sorted(entries, key=lambda name: (-(exact[name] - rounded[name]), name))
        for name in order[:shortfall]:
            rounded[name] += 1
        results = []
        for name in sorted(entries):
            if not entries[name]:
                continue
            exact_weight = float(entries[name]) * float(self.grams)
            actual = rounded[name] * self.precision
            results.append({
                "name": name, "grams": str(actual), "target_g": round(exact_weight, 6),
                "weighable": actual > 0, "quantization_g": round(float(actual) - exact_weight, 6),
            })
        return results

    def grid(self):
        result = []
        for row in range(self.row_steps):
            for col in range(self.column_steps):
                label = address(row, col)
                blend = self.blend(row, col)
                if blend is None:
                    result.append({"address": label, "status": "out_of_mix", "row": row, "column": col})
                    continue
                base, y, x, entries = blend
                weighted = self.round_weights(entries)
                result.append({
                    "address": label, "status": "weighable" if all(item["weighable"] for item in weighted) else "below_scale",
                    "row": row, "column": col, "fractions": [round(float(value), 6) for value in (base, y, x)],
                    "weights": weighted, "total_g": str(self.grams),
                })
        return result

    def as_json(self):
        return {
            "base": self.base.as_json(), "row_mix": self.row_mix.as_json(), "column_mix": self.column_mix.as_json(),
            "rows": self.row_steps, "columns": self.column_steps, "grams": str(self.grams),
            "precision": str(self.precision), "row_limit": float(self.row_limit) * 100,
            "column_limit": float(self.column_limit) * 100,
        }


def address(row, column):
    return f"{chr(65 + row)}{column + 1}"


def finish_readings(grid, photo, image, correction, data, store, read_photo):
    all_cells = {item["address"]: item for item in grid}
    details = {}
    shots = {photo["id"]: (photo, image, correction)}
    used = set()
    for item in seq(data.get("readings", []), "烧后观察", maximum=MAX_CELLS):
        item = obj(item, "烧后观察")
        slot = text(item.get("address"), "配方格", limit=4)
        if slot not in all_cells or all_cells[slot]["status"] == "out_of_mix":
            raise InputError(f"{slot}没有可观察的配方")
        if slot in details:
            raise InputError(f"{slot}已有一条烧后观察")
        photo_id = key(item.get("photo", photo["id"]), "成色图片")
        if photo_id not in shots:
            record = in_project(store.photo(photo_id), photo["firing_id"], "成色图片")
            shots[photo_id] = (record, read_photo(record), correction.identity())
        record, local, model = shots[photo_id]
        region = Region.read(item.get("region"), *local.size).as_json()
        label = optional_text(item.get("note"), "观察备注", 130)
        crop, _ = crop_region(local, region, max_side=126)
        values = analyze(local, region, model, bins=8)
        used.add(record["id"])
        details[slot] = (crop, {
            "photo_id": photo_id, "photo_sha256": record["png_sha"], "region": region,
            "note": label, "swatch": values["swatch"], "lab": values["lab"], "coverage_px": values["coverage_px"],
        })
    return details, sorted(used)


def recipe_map(mixes, grid, details):
    cell = 145
    cols, rows = mixes.column_steps, mixes.row_steps
    result = Image.new("RGB", (cols * cell + 50, rows * cell + 68), "#e7dfcd")
    draw = ImageDraw.Draw(result)
    draw.text((28, 13), "GLAZE TRIAL MATRIX  /  FIRED WITNESSES", fill="#453626")
    for record in grid:
        x, y = 25 + record["column"] * cell, 43 + record["row"] * cell
        content = details.get(record["address"])
        if content:
            picture, measured = content
            view = thumbnail(picture, 108)
            result.paste(flatten(view, (225, 216, 197)), (x + (cell - view.width) // 2, y + 3))
            draw.rectangle((x + 9, y + 102, x + cell - 10, y + 112), fill=measured["swatch"])
        else:
            background = "#cec4b2" if record["status"] == "out_of_mix" else "#ebe5d6"
            draw.rectangle((x + 9, y + 3, x + cell - 10, y + 112), fill=background)
            label = {"out_of_mix": "OUT OF MIX", "weighable": "AWAITING PHOTO", "below_scale": "BELOW SCALE"}[record["status"]]
            draw.text((x + 16, y + 48), label, fill="#655544")
        draw.text((x + 9, y + 120), record["address"], fill="#473c2d")
        if record.get("fractions"):
            fractions = "/".join(f"{value * 100:.0f}" for value in record["fractions"])
            draw.text((x + 47, y + 120), fractions, fill="#6d5845")
    return result


def resolve_refire(data, photo, image, correction, store):
    """Measure boxed target regions and collect stored witness cells, then plan."""
    block = obj(data.get("refire", data), "复烧选方")
    settings = dict(block)
    identity = correction.identity()
    measured_targets = []
    for item in seq(block.get("targets"), "要保的颜色", minimum=1, maximum=12):
        target = dict(obj(item, "要保颜色"))
        if target.get("lab") is None and target.get("region") is not None:
            model = correction if block.get("calibrated") else identity
            reading = analyze(image, target["region"], model, bins=8)
            target["lab"] = reading["lab"]
            target["swatch"] = reading["swatch"]
        measured_targets.append(target)
    settings["targets"] = measured_targets

    if settings.get("candidates") is None and settings.get("cells") is None:
        witnesses = []
        for record in store.entries(photo["firing_id"], kind="recipe"):
            witnesses.extend(candidates_from_entry(record))
        if not witnesses:
            raise InputError("还没有可对照的旧实烧格，请先归档返烧配方阵列或直接给出 candidates")
        settings["candidates"] = witnesses
    plan = plan_refire(settings)
    if block.get("weigh"):
        plan["weighing"] = recheck_weighing({**block["weigh"], "plan": plan.get("plan")})
    return plan


def matrix(photo, image, correction, settings, store, read_photo):
    data = obj(settings, "返烧配方表")
    # A refire-only request does not need a freshly hand-filled step matrix.
    if isinstance(data.get("refire"), dict) and data.get("base") is None:
        plan = resolve_refire(data, photo, image, correction, store)
        return {"refire": plan}, None, []
    mixes = TestGrid.read(data)
    cells = mixes.grid()
    details, sources = finish_readings(cells, photo, image, correction, data, store, read_photo)
    baseline = details.get("A1")
    for item in cells:
        observed = details.get(item["address"])
        if observed:
            reading = observed[1]
            item["reading"] = reading
            if baseline:
                reading["delta_e_from_a1"] = round(float(delta_e(reading["lab"], baseline[1]["lab"])), 4)
    result = {
        "mixes": mixes.as_json(), "cells": cells, "measured_cells": len(details),
        "usable_formulae": sum(item["status"] == "weighable" for item in cells),
        "ranges": {
            "maximum_axis_mix": [round(float(mixes.row_limit), 6), round(float(mixes.column_limit), 6)],
            "baseline_observed": baseline is not None,
        },
    }
    if isinstance(data.get("refire"), dict):
        result["refire"] = resolve_refire(data, photo, image, correction, store)
    return result, recipe_map(mixes, cells, details), sources
