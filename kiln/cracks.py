"""Join traced crack segments into whole cracks under confirmed observations."""

from dataclasses import dataclass
from itertools import product
import math

import numpy as np
from PIL import Image, ImageDraw

from .geometry import polygon_hits
from .inputs import (
    InputError, choice, coordinate, distinct, flag, integer, number, obj, optional_text, seq, text,
)


MAX_SEGMENTS = 48
MAX_JUNCTIONS = 20
MAX_OPTIONS = 12
MAX_PAIRS = 12
MAX_OBSERVATIONS = 30
MAX_CHECKS = 6
MAX_COMBINATIONS = 65536
STORED_PARTITIONS = 24
NATURES = ("ring", "through", "open")
NATURE_LABELS = {"ring": "闭环", "through": "通外", "open": "悬空"}
PALETTE = (
    "#e55542", "#5ab6e8", "#f9bf48", "#ad79e3", "#77cdb8", "#ef827f",
    "#9bd06a", "#e79996", "#60daec", "#d1a24f", "#8f9be8", "#e8a05a",
)


@dataclass(frozen=True)
class Segment:
    name: str
    points: tuple
    closed: bool
    origin: str

    @classmethod
    def read(cls, value, width, height):
        raw = obj(value, "裂纹段")
        name = text(raw.get("name"), "段名", limit=40)
        closed = flag(raw.get("closed", False), "段闭合")
        points = tuple(
            coordinate(point, "段轨迹")
            for point in seq(raw.get("points"), "段轨迹", minimum=3 if closed else 2, maximum=40)
        )
        for x, y in points:
            if not (0 <= x <= width and 0 <= y <= height):
                raise InputError(f"段「{name}」的轨迹超出照片")
        return cls(name, points, closed, "inline")

    @property
    def length(self):
        travel = math.fsum(math.dist(a, b) for a, b in zip(self.points, self.points[1:]))
        if self.closed:
            travel += math.dist(self.points[-1], self.points[0])
        return travel

    def end_point(self, side):
        return self.points[0] if side == 0 else self.points[-1]


def segments_from_marks(payload, width, height):
    result = payload.get("result", {}) if isinstance(payload, dict) else {}
    found = []
    for mark in result.get("marks", []):
        if not isinstance(mark, dict) or mark.get("kind") not in ("crack", "unknown"):
            continue
        shape = mark.get("shape")
        if shape not in ("trace", "polygon"):
            continue
        points = tuple((float(point[0]), float(point[1])) for point in mark.get("points") or [])
        if len(points) < (3 if shape == "polygon" else 2):
            continue
        if any(not (0 <= x <= width and 0 <= y <= height) for x, y in points):
            continue
        found.append(Segment(str(mark.get("name"))[:40], points, shape == "polygon", "marks"))
    return found


def read_end(value, segments):
    parts = seq(value, "线段端点", minimum=2, maximum=2)
    segment = integer(parts[0], "端点段序号", low=0, high=len(segments) - 1)
    side = integer(parts[1], "端点侧", low=0, high=1)
    if segments[segment].closed:
        raise InputError(f"段「{segments[segment].name}」是闭合段，没有可接的端点")
    return (segment, side)


@dataclass(frozen=True)
class Junction:
    name: str
    options: tuple

    @classmethod
    def read(cls, value, position, segments):
        raw = obj(value, "交点")
        name = optional_text(raw.get("name"), "交点名", 40) or f"J{position}"
        seen = set()
        options = []
        for raw_option in seq(raw.get("options"), f"交点{name}的接法", minimum=1, maximum=MAX_OPTIONS):
            pairs = []
            for raw_pair in seq(raw_option, f"交点{name}的接法", minimum=0, maximum=MAX_PAIRS):
                ends = seq(raw_pair, "接点对", minimum=2, maximum=2)
                pair = tuple(sorted((read_end(ends[0], segments), read_end(ends[1], segments))))
                if pair[0] == pair[1]:
                    raise InputError(f"交点{name}的接点对两端相同")
                pairs.append(pair)
            used = [end for pair in pairs for end in pair]
            if len(set(used)) != len(used):
                raise InputError(f"交点{name}的一种接法里同一端被接了两次")
            normalized = tuple(sorted(pairs))
            if normalized not in seen:
                seen.add(normalized)
                options.append(normalized)
        return cls(name, tuple(options))

    @property
    def ends(self):
        return {end for option in self.options for pair in option for end in pair}


@dataclass(frozen=True)
class Observation:
    kind: str
    segments: tuple

    @classmethod
    def read(cls, value, count):
        raw = obj(value, "观察")
        kind = choice(raw.get("type"), "观察类型", ("same", "different", "outside"))
        members = tuple(
            integer(item, "观察段序号", low=0, high=count - 1)
            for item in seq(raw.get("segments"), "观察段", minimum=1 if kind == "outside" else 2, maximum=8)
        )
        distinct(members, "观察段")
        return cls(kind, members)


class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, first, second):
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            self.parent[second_root] = first_root


def consumed_ends(junctions, combo):
    consumed = {}
    for junction, option_index in zip(junctions, combo):
        for first, second in junction.options[option_index]:
            consumed[first] = second
            consumed[second] = first
    return consumed


def reaches_border(point, margin, width, height):
    x, y = point
    return x <= margin or y <= margin or x >= width - margin or y >= height - margin


def build_partition(segments, junctions, combo, margin, width, height):
    consumed = consumed_ends(junctions, combo)
    sets = UnionFind(len(segments))
    for first, second in consumed.items():
        sets.union(first[0], second[0])
    groups = {}
    for member in range(len(segments)):
        groups.setdefault(sets.find(member), []).append(member)
    cracks = []
    for members in sorted(groups.values(), key=lambda group: min(group)):
        free = tuple(
            (member, side) for member in members if not segments[member].closed
            for side in (0, 1) if (member, side) not in consumed
        )
        if not free:
            nature = "ring"
        elif any(reaches_border(segments[member].end_point(side), margin, width, height)
                 for member, side in free):
            nature = "through"
        else:
            nature = "open"
        cracks.append({"members": tuple(members), "free": free, "nature": nature})
    owners = {}
    for index, crack in enumerate(cracks):
        for member in crack["members"]:
            owners[member] = index
    return {"cracks": cracks, "owners": owners, "consumed": consumed}


def satisfies(partition, observations):
    cracks = partition["cracks"]
    for observation in observations:
        targets = {partition["owners"][member] for member in observation.segments}
        if observation.kind == "same" and len(targets) != 1:
            return False
        if observation.kind == "different" and len(targets) != len(observation.segments):
            return False
        if observation.kind == "outside" and not any(
                cracks[target]["nature"] == "through" for target in targets):
            return False
    return True


def order_component(members, consumed, segments):
    closed = tuple(member for member in members if segments[member].closed)
    open_members = [member for member in members if not segments[member].closed]
    if not open_members:
        return (), closed
    start = None
    for member in open_members:
        for side in (0, 1):
            if (member, side) not in consumed:
                start = (member, side)
                break
        if start is not None:
            break
    if start is None:
        start = (open_members[0], 0)
    order = []
    visited = set()
    current = start
    while current is not None and current[0] not in visited:
        member, side = current
        visited.add(member)
        order.append(current)
        current = consumed.get((member, 1 - side))
        if current == start:
            break
    return tuple(order), closed


def strand_points(order, segments):
    points = []
    for member, side in order:
        segment = segments[member].points
        points.extend(segment if side == 0 else segment[::-1])
    return points


def crack_polygon(crack, consumed, segments):
    order, closed = order_component(crack["members"], consumed, segments)
    points = strand_points(order, segments)
    for member in closed:
        points.extend(segments[member].points)
    return points


def encloses(polygon_points, point):
    if len(polygon_points) < 3:
        return False
    hit = polygon_hits(list(polygon_points), np.array([point[0]]), np.array([point[1]]))
    return bool(hit[0])


def solve(segments, junctions, observations, margin, width, height, checks):
    combinations = 1
    for junction in junctions:
        combinations *= len(junction.options)
    if combinations > MAX_COMBINATIONS:
        raise InputError(f"接法组合共 {combinations} 种，超过上限 {MAX_COMBINATIONS}，请减少交点或每个交点的接法")
    valid = []
    for combo in product(*(range(len(junction.options)) for junction in junctions)):
        partition = build_partition(segments, junctions, combo, margin, width, height)
        if not satisfies(partition, observations):
            continue
        enclosure = {index: [] for index in range(len(checks))}
        for crack_index, crack in enumerate(partition["cracks"]):
            if not checks or crack["nature"] != "ring":
                continue
            polygon_points = crack_polygon(crack, partition["consumed"], segments)
            for index, point in enumerate(checks):
                if encloses(polygon_points, point):
                    enclosure[index].append(crack_index)
        valid.append({
            "options": combo,
            "cracks": partition["cracks"],
            "owners": partition["owners"],
            "enclosure": enclosure,
        })
    return combinations, valid


def focus_report(valid, adopted, focus, segments, junctions):
    readings = {}
    for index, partition in enumerate(valid):
        nature = partition["cracks"][partition["owners"][focus]]["nature"]
        readings.setdefault(nature, []).append(index)
    adopted_partition = valid[adopted]
    adopted_nature = adopted_partition["cracks"][adopted_partition["owners"][focus]]["nature"]
    base_members = set(adopted_partition["cracks"][adopted_partition["owners"][focus]]["members"])
    others = []
    for nature in NATURES:
        indices = readings.get(nature)
        if not indices or nature == adopted_nature:
            continue
        extra_segments = set()
        changed_junctions = set()
        for index in indices:
            partition = valid[index]
            members = partition["cracks"][partition["owners"][focus]]["members"]
            extra_segments |= set(members) - base_members
            changed_junctions |= {
                position for position, pair in enumerate(zip(partition["options"], adopted_partition["options"]))
                if pair[0] != pair[1]
            }
        others.append({
            "nature": nature,
            "nature_label": NATURE_LABELS[nature],
            "partition_count": len(indices),
            "segments_not_photographed": [segments[member].name for member in sorted(extra_segments)],
            "junctions_not_photographed": [junctions[position].name for position in sorted(changed_junctions)],
        })
    return {
        "segment_index": focus,
        "segment": segments[focus].name,
        "default_nature": adopted_nature,
        "nature_label": NATURE_LABELS[adopted_nature],
        "readings": [
            {
                "nature": nature,
                "nature_label": NATURE_LABELS[nature],
                "partition_count": len(readings[nature]),
                "partitions": readings[nature][:60],
                "adopted": nature == adopted_nature,
            }
            for nature in NATURES if nature in readings
        ],
        "other_positions": others,
    }


def checks_report(valid, checks):
    individual = []
    for index in range(len(checks)):
        hits = [position for position, partition in enumerate(valid) if partition["enclosure"].get(index)]
        individual.append({"point": index, "partition_count": len(hits), "partitions": hits[:60]})
    pairs = []
    for first in range(len(checks)):
        for second in range(first + 1, len(checks)):
            joint, shared = [], []
            for position, partition in enumerate(valid):
                enclosure = partition["enclosure"]
                first_rings = enclosure.get(first, [])
                second_rings = enclosure.get(second, [])
                if any(a != b for a in first_rings for b in second_rings):
                    joint.append(position)
                if set(first_rings) & set(second_rings):
                    shared.append(position)
            pairs.append({
                "points": [first, second],
                "joint": bool(joint),
                "partitions": joint[:60],
                "same_ring_partitions": shared[:60],
            })
    return {"points": [list(point) for point in checks], "individual": individual, "pairs": pairs}


def chip(draw, x, y, label, color):
    width = 8 + 8 * len(label)
    draw.rounded_rectangle((x, y, x + width, y + 17), radius=3, fill="#211c16")
    draw.text((x + 4, y + 1), label, fill=color)


def draw_partition(image, segments, junctions, junction_states, partition, checks, focus, margin):
    scale = min(1, 1600 / max(image.size))
    base = image.resize(
        tuple(max(1, round(value * scale)) for value in image.size), Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", base.size)
    draw = ImageDraw.Draw(layer)
    width, height = image.size
    if partition is None:
        for index, segment in enumerate(segments, 1):
            path = [(x * scale, y * scale) for x, y in segment.points]
            if segment.closed:
                draw.polygon(path, outline="#8f8578", width=3)
            else:
                draw.line(path, fill="#8f8578", width=3, joint="curve")
            chip(draw, path[0][0] + 5, path[0][1] - 21, f"S{index}", "#8f8578")
    else:
        consumed = consumed_ends(junctions, partition["options"])
        for crack_index, crack in enumerate(partition["cracks"], 1):
            color = PALETTE[(crack_index - 1) % len(PALETTE)]
            points = crack_polygon(crack, consumed, segments)
            path = [(x * scale, y * scale) for x, y in points]
            highlighted = focus is not None and focus in crack["members"]
            if highlighted and len(path) > 1:
                draw.line(path, fill="#fff7e8", width=9, joint="curve")
            if crack["nature"] == "ring" and len(path) >= 3:
                draw.polygon(path, fill=color + "3d", outline=color, width=4)
            elif len(path) > 1:
                draw.line(path, fill=color, width=4, joint="curve")
            for member, side in crack["free"]:
                ex, ey = segments[member].end_point(side)
                ex, ey = ex * scale, ey * scale
                if reaches_border((ex / scale, ey / scale), margin, width, height):
                    draw.rectangle((ex - 5, ey - 5, ex + 5, ey + 5), outline=color, width=2)
                else:
                    draw.ellipse((ex - 5, ey - 5, ex + 5, ey + 5), outline=color, width=2)
            if path:
                cx = sum(point[0] for point in path) / len(path)
                cy = sum(point[1] for point in path) / len(path)
                chip(draw, cx + 5, cy - 21, f"C{crack_index}", color)
    for position, junction in enumerate(junctions, 1):
        ends = junction.ends
        if not ends:
            continue
        jx = sum(segments[member].end_point(side)[0] for member, side in ends) / len(ends) * scale
        jy = sum(segments[member].end_point(side)[1] for member, side in ends) / len(ends) * scale
        decided = junction_states[position - 1]["decided"]
        chip(draw, jx + 6, jy + 6, f"J{position}" if decided else f"J{position}?",
             "#96b6a2" if decided else "#f9bf48")
    for index, point in enumerate(checks, 1):
        x, y = point[0] * scale, point[1] * scale
        draw.line(((x - 9, y), (x + 9, y)), fill="#ff4fd8", width=3)
        draw.line(((x, y - 9), (x, y + 9)), fill="#ff4fd8", width=3)
        draw.text((x + 8, y - 18), str(index), fill="#ff4fd8", stroke_width=1, stroke_fill="#211c16")
    base.alpha_composite(layer)
    return base


def consolidate(image, settings, ruler, marks_payload=None):
    data = obj(settings, "裂纹归并")
    width, height = image.size
    segments = []
    if marks_payload is not None:
        segments.extend(segments_from_marks(marks_payload, width, height))
    segments.extend(
        Segment.read(item, width, height)
        for item in seq(data.get("segments") or [], "裂纹段", maximum=MAX_SEGMENTS)
    )
    if not segments:
        raise InputError("至少需要一段裂纹：在 segments 里给出，或用 source_marks 引用当前照片的缺陷图记录")
    if len(segments) > MAX_SEGMENTS:
        raise InputError(f"裂纹段最多 {MAX_SEGMENTS} 段")
    distinct([segment.name.casefold() for segment in segments], "段名")
    junctions = [
        Junction.read(item, position, segments)
        for position, item in enumerate(seq(data.get("junctions") or [], "交点", maximum=MAX_JUNCTIONS), 1)
    ]
    seen_ends = {}
    for junction in junctions:
        for end in junction.ends:
            other = seen_ends.get(end)
            if other is not None:
                raise InputError(f"段 {end[0]} 的端点同时出现在交点{other}和交点{junction.name}")
            seen_ends[end] = junction.name
    observations = [
        Observation.read(item, len(segments))
        for item in seq(data.get("observations") or [], "观察", maximum=MAX_OBSERVATIONS)
    ]
    focus = data.get("focus")
    if focus is not None:
        focus = integer(focus, "疑点段序号", low=0, high=len(segments) - 1)
    prefer = data.get("prefer")
    if prefer is not None:
        prefer = integer(prefer, "采用分法序号", low=0, high=10 ** 6)
    checks = []
    for item in seq(data.get("checks") or [], "检查点", maximum=MAX_CHECKS):
        point = coordinate(item, "检查点")
        if not (0 <= point[0] <= width and 0 <= point[1] <= height):
            raise InputError("检查点超出照片")
        checks.append(point)
    margin = number(data.get("margin", 24), "通到外边距", low=0, high=600)

    combinations, valid = solve(segments, junctions, observations, margin, width, height, checks)
    if prefer is not None and prefer >= len(valid):
        raise InputError(f"第 {prefer} 份合格分法不存在，当前只有 {len(valid)} 份")
    if valid:
        if prefer is not None:
            adopted = prefer
        elif focus is not None:
            counts = {nature: 0 for nature in NATURES}
            for partition in valid:
                counts[partition["cracks"][partition["owners"][focus]]["nature"]] += 1
            majority = min(NATURES, key=lambda nature: (-counts[nature], NATURES.index(nature)))
            adopted = next(
                index for index, partition in enumerate(valid)
                if partition["cracks"][partition["owners"][focus]]["nature"] == majority
            )
        else:
            adopted = 0
    else:
        adopted = None

    junction_states = []
    for position, junction in enumerate(junctions):
        kept = sorted({partition["options"][position] for partition in valid})
        junction_states.append({
            "label": f"J{position + 1}",
            "name": junction.name,
            "decided": len(kept) == 1,
            "kept_options": kept,
            "option_count": len(junction.options),
        })

    shown = []
    order = ([adopted] + [index for index in range(len(valid)) if index != adopted]) if valid else []
    for index in order[:STORED_PARTITIONS]:
        partition = valid[index]
        cracks = []
        for crack_index, crack in enumerate(partition["cracks"], 1):
            length = math.fsum(segments[member].length for member in crack["members"])
            entry = {
                "label": f"C{crack_index}",
                "nature": crack["nature"],
                "nature_label": NATURE_LABELS[crack["nature"]],
                "segments": [segments[member].name for member in crack["members"]],
                "length_px": round(length, 4),
            }
            if ruler is not None:
                entry["length_mm"] = round(length * ruler.per_pixel, 4)
            cracks.append(entry)
        shown.append({"index": index, "options": list(partition["options"]), "cracks": cracks})

    notes = [f"共 {combinations} 种接法组合，其中 {len(valid)} 种分法同时满足全部已确认观察。"]
    if not valid:
        notes.append("没有任何分法能同时满足全部观察，请核对同条、异条与通到外边的观察是否互相冲突。")
    notes.append("每段在任一分法中恰好归入一根裂纹，不会重复计入两根。")
    for state in junction_states:
        if not state["decided"]:
            notes.append(f"交点{state['name']}的接法排不定，保留选项 {state['kept_options']}（按输入顺序从 0 计）。")

    result = {
        "segment_count": len(segments),
        "segments": [
            {
                "label": f"S{index + 1}", "name": segment.name, "origin": segment.origin,
                "closed": segment.closed, "length_px": round(segment.length, 4),
            }
            for index, segment in enumerate(segments)
        ],
        "junction_count": len(junctions),
        "observation_count": len(observations),
        "combinations": combinations,
        "valid": len(valid),
        "contradiction": not valid,
        "margin_px": margin,
        "junctions": junction_states,
        "adopted": adopted,
        "partitions": shown,
        "partitions_shown": len(shown),
    }

    if focus is not None and valid:
        focus_data = focus_report(valid, adopted, focus, segments, junctions)
        result["focus"] = focus_data
        notes.append(
            f"疑点段「{focus_data['segment']}」默认按{focus_data['nature_label']}处理"
            f"（采用第 {adopted} 份合格分法）；把参数 prefer 改成另一份分法序号即可更换。"
        )
        for other in focus_data["other_positions"]:
            segments_text = "、".join(other["segments_not_photographed"]) or "无"
            junctions_text = "、".join(other["junctions_not_photographed"]) or "无"
            notes.append(
                f"另一路（{other['nature_label']}）这次不按它拍的位置：段 {segments_text}；交点 {junctions_text}。"
            )
    if checks:
        result["checks"] = checks_report(valid, checks)
        if not valid:
            notes.append("没有合格分法，无法评估检查点的闭环包住情况。")
        for pair in result["checks"]["pairs"]:
            first, second = pair["points"]
            if pair["joint"]:
                notes.append(
                    f"检查点 {first + 1}、{second + 1} 能在同一份分法里各被一条不同的闭环包住"
                    f"（例如第 {pair['partitions'][:5]} 份）。"
                )
            else:
                notes.append(f"检查点 {first + 1}、{second + 1} 没有任何分法能让两处同时各被一条不同的闭环包住。")
    result["notes"] = notes

    partition = None
    if valid:
        partition = {"options": valid[adopted]["options"], "cracks": valid[adopted]["cracks"]}
    picture = draw_partition(image, segments, junctions, junction_states, partition, checks, focus, margin)
    return result, picture
