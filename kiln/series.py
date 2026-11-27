"""Track which measurements remain comparable across successive firings."""

from dataclasses import dataclass
from datetime import date
from itertools import groupby

from PIL import Image, ImageDraw

from .calibration import delta_e
from .inputs import InputError, choice, distinct, key, number, obj, optional_text, seq


FEATURES = {
    "color_shift": {"kind": "color", "units": "Delta E 76", "name": "相对基准的色差"},
    "glare_fraction": {"kind": "glare", "units": "%", "name": "高光像素占比"},
    "fine_texture": {"kind": "texture", "units": "RMS", "name": "细质地能量"},
    "spot_contrast": {"kind": "spot", "units": "Delta E 76", "name": "斑点与周边色差"},
    "pinhole_density": {"kind": "pinholes", "units": "/ cm2", "name": "每平方厘米针孔数"},
    "crazing_density": {"kind": "crazing", "units": "mm / cm2", "name": "每平方厘米纹线长度"},
}


@dataclass(frozen=True)
class Reading:
    source: dict
    firing: dict
    photo: dict | None
    track: str
    nick: str

    @property
    def result(self):
        return self.source["payload"]["result"]

    @property
    def preferences(self):
        return self.source["payload"]["settings"]

    @property
    def sensor(self):
        return self.source["payload"].get("models", {}).get("calibration") is not None

    @property
    def chronology(self):
        return self.firing["fired_on"] or self.source["created"][:10]

    def summary(self):
        return {
            "entry_id": self.source["id"], "entry_revision": self.source["revision"], "firing_id": self.firing["id"],
            "firing_name": self.firing["name"], "photo_id": self.photo["id"] if self.photo else None,
            "photo_name": self.photo["title"] if self.photo else None, "chronology": self.chronology,
            "dated_from": "firing" if self.firing["fired_on"] else "archive",
            "name": self.nick, "track": self.track, "calibrated": self.sensor,
        }


def readings(store, raw, mode):
    desired = FEATURES[mode]["kind"]
    result = []
    cache = {}
    for item in seq(raw, "趋势记录", minimum=2, maximum=32):
        spec = obj(item, "趋势记录")
        source = store.entry(key(spec.get("entry"), "趋势记录编号"))
        if source["kind"] != desired:
            raise InputError(f"{source['title']}不是这个指标所需的分析类型")
        batch = source["firing_id"]
        if batch not in cache:
            cache[batch] = store.firing(batch)
        photo = store.photo(source["photo_id"]) if source["photo_id"] else None
        track = optional_text(spec.get("track"), "比较组名", 45) or "整体"
        nick = optional_text(spec.get("label"), "测点名", 48) or source["title"]
        result.append(Reading(source, cache[batch], photo, track, nick))
    distinct([item.source["id"] for item in result], "趋势记录")
    order = lambda item: (item.track, item.chronology, item.source["id"])
    return sorted(result, key=order)


def comparator_signature(item, mode):
    raw = item.preferences
    results = item.result
    basis = [item.sensor]
    if mode == "fine_texture":
        basis.extend(scale["radius_px"] for scale in results["scales"])
    if mode == "pinhole_density":
        for key_name, default in (("contrast", 0.11), ("neighborhood", 5),
                                  ("minimum_area", 3), ("maximum_area", 110), ("connectivity", "eight")):
            basis.append(raw.get(key_name, default))
    if mode == "glare_fraction":
        for key_name, default in (("brightness", 0.81), ("max_chroma", 14), ("contrast", 0.08), ("neighborhood", 10)):
            basis.append(raw.get(key_name, default))
    if mode == "crazing_density":
        for key_name, default in (("floor", 0.018), ("scale", 1.45), ("contrast", 0.045),
                                  ("minimum", 12), ("bridge", True), ("edge", 2)):
            basis.append(raw.get(key_name, default))
    return tuple(basis)


def quantify(item, reference, mode):
    entry = item.result
    if mode == "color_shift":
        return float(delta_e(entry["lab"], reference.result["lab"])), None
    if mode == "glare_fraction":
        return entry["fraction"] * 100, None
    if mode == "spot_contrast":
        return entry["center_delta_e"], None
    if mode == "fine_texture":
        return entry["scales"][0]["energy_rms"], None
    if mode == "crazing_density":
        size = entry.get("sample_area_mm2")
        if not size:
            return None, "建立照片标尺后才有面积单位"
        return entry["summary_length_mm"] * 100 / size, None
    scale = entry.get("unit_mm_per_px")
    if not scale:
        return None, "建立照片标尺后才有面积单位"
    return entry["count"] * 100 / (entry["coverage_px"] * scale ** 2), None


def observe(group, mode, policy, explicit_baseline, tolerance):
    anchor = group[0]
    if explicit_baseline:
        selected = [item for item in group if item.source["id"] == explicit_baseline]
        if selected:
            anchor = selected[0]
    state = comparator_signature(anchor, mode)
    values = []
    warnings = 0
    baseline_value, _ = quantify(anchor, anchor, mode)
    for item in group:
        reason = ""
        value, problem = quantify(item, anchor, mode)
        same = comparator_signature(item, mode) == state
        if not same:
            reason = "这张照片的校色状态或工具测量参数和基准不同"
            if policy == "strict":
                value = None
        if problem:
            reason = problem
        if reason:
            warnings += 1
        if value is not None and baseline_value is not None:
            deviation = round(value - baseline_value, 6)
        else:
            deviation = None
        values.append({
            **item.summary(), "value": round(value, 6) if value is not None else None,
            "deviation": deviation, "same_measurement_settings": same,
            "note": reason or None, "baseline": item.source["id"] == anchor.source["id"],
            "out_of_tolerance": abs(deviation) > tolerance if deviation is not None else None,
        })
    return values, warnings


def day_position(label):
    return date.fromisoformat(label).toordinal()


def series_plot(values, mode, tolerance):
    w, h = 940, 380
    canvas = Image.new("RGB", (w, h), "#eee5d5")
    draw = ImageDraw.Draw(canvas)
    x0, y0, x1, y1 = 64, 49, 906, 315
    draw.text((x0, 21), f"FIRING READINGS  /  {FEATURES[mode]['units']}", fill="#4b3929")
    date_numbers = [day_position(item["chronology"]) for item in values]
    lo, hi = min(date_numbers), max(date_numbers)
    samples = [item["value"] for item in values if item["value"] is not None]
    min_v = min(samples) if samples else 0.0
    max_v = max(samples) if samples else 1.0
    delta = max(max_v - min_v, abs(max_v) * 0.09, 0.01) * 0.12
    low, high = min_v - delta, max_v + delta

    def at_x(value):
        if lo == hi:
            return (x0 + x1) / 2
        return x0 + (x1 - x0) * (day_position(value) - lo) / (hi - lo)

    def at_y(value):
        return y1 - (y1 - y0) * (value - low) / (high - low)

    for index in range(6):
        amount = low + (high - low) * index / 5
        ypos = at_y(amount)
        draw.line((x0, ypos, x1, ypos), fill="#d1c3ae")
        draw.text((7, ypos - 6), f"{amount:.3g}", fill="#6a5744")

    palettes = ("#8d4830", "#966c23", "#3c7562", "#56799c", "#7b5b7c")
    for ix, (track, entries) in enumerate(groupby(values, key=lambda item: item["track"])):
        color = palettes[ix % len(palettes)]
        before = None
        for item in entries:
            x = at_x(item["chronology"])
            if before is not None:
                gap = x - before[0]
                if gap == 0:
                    x += 3
            if item["value"] is None:
                draw.line((x - 3, y1 + 4, x + 3, y1 + 10), fill="#877663", width=1)
                draw.line((x + 3, y1 + 4, x - 3, y1 + 10), fill="#877663", width=1)
                before = None
                continue
            y = at_y(item["value"])
            if before is not None:
                draw.line((*before, x, y), fill=color, width=2)
            dot = color if item["same_measurement_settings"] else "#f6ebdb"
            outline = "#b12d19" if item["out_of_tolerance"] else color
            draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=dot, outline=outline, width=2)
            before = x, y
        draw.rectangle((x0 + ix * 85, y1 + 29, x0 + ix * 85 + 14, y1 + 35), fill=color)
        draw.text((x0 + ix * 85 + 18, y1 + 25), f"GROUP {ix + 1}", fill="#513e2b")
    draw.text((x0, h - 14), date.fromordinal(lo).isoformat(), fill="#513e2b")
    draw.text((x1 - 65, h - 14), date.fromordinal(hi).isoformat(), fill="#513e2b")
    draw.text((x1 - 120, 17), f"TOL {tolerance:.3g}", fill="#695341")
    return canvas


def build_series(store, user):
    raw = obj(user, "窑次趋势")
    mode = choice(raw.get("feature", "color_shift"), "走势指标", tuple(FEATURES))
    policy = choice(raw.get("policy", "strict"), "参数不一致处理", ("strict", "annotate"))
    tolerance = number(raw.get("tolerance", 5), "观察区别阈值", low=0.001, high=4000)
    all_entries = readings(store, raw.get("readings"), mode)
    explicit = raw.get("baseline")
    if explicit and not any(item.source["id"] == explicit for item in all_entries):
        raise InputError("应在所选记录中指定基准")
    values = []
    warn_count = 0
    groups = []
    for track, group in groupby(all_entries, key=lambda item: item.track):
        items = list(group)
        result, warnings = observe(items, mode, policy, explicit, tolerance)
        warn_count += warnings
        groups.append({"name": track, "count": len(result), "warn": warnings})
        values.extend(result)
    if len(groups) > 6:
        raise InputError("趋势图最多六个比较组")
    computed = {
        "feature": mode, **FEATURES[mode], "rows": values, "tolerance": tolerance, "policy": policy,
        "groups": groups, "warning_count": warn_count, "valid_count": sum(x["value"] is not None for x in values),
    }
    return computed, series_plot(values, mode, tolerance)
