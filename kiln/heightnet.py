"""Combine horizontal and vertical grid slope readings into one closed height map.

The survey network is a small grid of stations.  Every adjacent pair carries a
signed height-difference (derived from the corresponding grazing-light slope)
and an acceptable error, so an observation is an interval
``value +/- tolerance``.  A retained set of observations is usable when one
single assignment of station heights satisfies every interval: walking around
any cell (or any loop) returns to the same height.

The solver keeps every required segment first, drops ("shelves") the minimum
conflicting optional edges through an interval difference-constraints graph,
and always offers a second, equally feasible map that performs the mirror
swap (the narrow-lip / wide-base trade-off in the field story).

For two queried stations the exact projection of *all* feasible height maps is
``[lower, upper]`` obtained from all-pairs shortest paths on the constraint
graph; shelved direct corridor edges are reported as missing direct slope
data.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .inputs import InputError, number

EPS = 1e-8
INF = math.inf

HORIZONTAL_WORDS = {"h", "horiz", "horizontal", "x", "col", "column", "columns", "横", "横向", "水平"}
VERTICAL_WORDS = {"v", "vert", "vertical", "y", "row", "rows", "行", "纵向", "竖直", "纵"}

LIP_WORDS = ("唇", "lip", "rim")
BASE_WORDS = ("底", "base", "bottom", "foot")


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

def _first(data: dict, keys: Iterable[str], default=None):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return default


def _as_float(value):
    if value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


NETWORK_MARKERS = ("rows", "columns", "horizontal", "vertical", "edges", "readings",
                   "segments", "station_grid", "height_network")


def is_network_payload(settings: Any) -> bool:
    """Recognise grid-survey settings sent through the relief entry point."""
    if not isinstance(settings, dict):
        return False
    if str(settings.get("mode", "")).lower() in {"network", "heightmap", "grid", "synthesis"}:
        return True
    if settings.get("height_network") is True:
        return True
    if isinstance(settings.get("horizontal"), list) or isinstance(settings.get("vertical"), list):
        return True
    return any(isinstance(settings.get(key), list) and settings.get(key)
               for key in ("edges", "readings", "segments"))


def _orientation(value: Any) -> str:
    if value is None:
        raise InputError("测边需要方向 horizontal/vertical", field="orientation")
    token = str(value).strip().lower()
    if token in {w.lower() for w in HORIZONTAL_WORDS}:
        return "H"
    if token in {w.lower() for w in VERTICAL_WORDS}:
        return "V"
    raise InputError(f"不认识的测边方向：{value}", field="orientation")


def _domain_tag(label: str, group: Any = None) -> str:
    text = f"{label or ''} {group or ''}".lower()
    if any(word in text for word in LIP_WORDS):
        return "lip"
    if any(word in text for word in BASE_WORDS):
        return "base"
    return "ordinary"


def _edge_id(orientation: str, row: int, col: int) -> str:
    return f"{orientation}:{row}:{col}"


def _compact_id(token: str) -> str | None:
    """Accept spellings such as H0-1, h[0][1], H01 and normalise to H:0:1."""
    text = str(token).strip()
    if not text:
        return None
    head = text[0].upper()
    if head not in "HV":
        return None
    digits = "".join(ch if (ch.isdigit() or ch in ":-") else " " for ch in text[1:])
    parts = [p for p in digits.replace(":", " ").replace("-", " ").split() if p.isdigit()]
    if len(parts) < 2:
        runs = []
        current = ""
        for ch in text[1:]:
            if ch.isdigit():
                current += ch
            elif current:
                runs.append(current)
                current = ""
        if current:
            runs.append(current)
        if len(runs) >= 2:
            parts = runs[:2]
    if len(parts) < 2:
        return None
    return _edge_id(head, int(parts[0]), int(parts[1]))


def edge_matches(spec: Any, edge: dict) -> bool:
    if isinstance(spec, str):
        token = spec.strip()
        if token == edge["id"] or token.lower() == edge["id"].lower():
            return True
        compact = _compact_id(token)
        if compact and compact == edge["id"]:
            return True
        if edge.get("label") and token == edge["label"]:
            return True
        return False
    if isinstance(spec, (list, tuple)) and len(spec) == 2 and all(isinstance(x, int) for x in spec):
        return (edge["row"], edge["col"]) == (int(spec[0]), int(spec[1]))
    if isinstance(spec, dict):
        orientation = spec.get("orientation") or spec.get("dir")
        if orientation is not None:
            try:
                if _orientation(orientation) != edge["orientation"]:
                    return False
            except InputError:
                return False
        row = _first(spec, ("row", "r"))
        col = _first(spec, ("column", "col", "c", "index"))
        at = spec.get("at") or spec.get("position")
        if at and len(at) == 2:
            row, col = at[0], at[1]
        if row is not None and int(row) != edge["row"]:
            return False
        if col is not None and int(col) != edge["col"]:
            return False
        if spec.get("id") and str(spec["id"]) != edge["id"]:
            return False
        if spec.get("label") and spec["label"] != edge.get("label"):
            return False
        return row is not None or col is not None or spec.get("id") or spec.get("label")
    return False


def _convert_value(value: float, data: dict, edge_data: dict, spacing: float | None) -> float:
    kind = str(_first(edge_data, ("value_kind", "kind", "measure"),
                      _first(data, ("value_kind", "reading_kind"), "difference"))).lower()
    if kind in {"difference", "delta", "dh", "height", "height_difference", "高差", "坡度差"}:
        return value
    bay = _first(edge_data, ("spacing", "distance", "bay", "length", "span"),
                 _first(data, ("station_spacing", "spacing", "bay", "distance")))
    bay = float(bay) if bay is not None else (spacing if spacing is not None else 1.0)
    if kind in {"ratio", "slope_ratio"}:
        return value * bay
    if kind in {"percent", "pct", "slope_percent", "%"}:
        return value / 100.0 * bay
    if kind in {"degree", "deg", "angle", "angle_deg", "slope_degrees"}:
        return math.tan(math.radians(value)) * bay
    return value


def _edge_from_cell(cell: Any, orientation: str, row: int, col: int,
                    data: dict, default_tol: float, spacing: float | None,
                    index: int) -> dict:
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        raw = {"value": float(cell)}
    elif isinstance(cell, dict):
        raw = cell
    elif isinstance(cell, (list, tuple)) and cell:
        raw = {"value": cell[0]}
        if len(cell) > 1:
            raw["tolerance"] = cell[1]
    else:
        raise InputError(f"测边 {_edge_id(orientation, row, col)} 的读数格式不正确")

    provided_id = raw.get("id") or raw.get("key")
    eid = str(provided_id) if provided_id else _edge_id(orientation, row, col)
    value = _first(raw, ("value", "slope", "delta", "dh", "difference", "height_difference",
                         "rise", "gradient", "reading", "v", "z"))
    if value is None:
        degree_value = _first(raw, ("slope_degrees", "angle_degrees", "tilt_degrees", "angle", "deg"))
        percent_value = _first(raw, ("slope_percent", "percent", "pct"))
        if degree_value is not None:
            value = degree_value
            raw = {**raw, "value_kind": "angle_deg"}
        elif percent_value is not None:
            value = percent_value
            raw = {**raw, "value_kind": "percent"}
    if value is None:
        raise InputError(f"测边 {eid} 缺少读数值")
    value = _convert_value(number(value, f"测边 {eid} 读数"), data, raw, spacing)

    low = _first(raw, ("lower", "lo", "min", "minimum"))
    high = _first(raw, ("upper", "hi", "max", "maximum"))
    tol_raw = _first(raw, ("tolerance", "error", "eps", "uncertainty", "acceptable_error", "sigma"))
    tol = float(tol_raw) if tol_raw is not None else None
    if low is not None and high is not None:
        lo, hi = float(low), float(high)
        if raw.get("value") is None:
            value = (lo + hi) / 2.0
    else:
        tol = tol if tol is not None else default_tol
        lo, hi = value - tol, value + tol

    required = bool(_first(raw, ("required", "must_keep", "keep", "mandatory", "preserve"), False))
    label = _first(raw, ("label", "name", "title"), None)
    group = _first(raw, ("group", "feature", "zone", "part"), None)
    support = _first(raw, ("support", "weight", "confidence", "samples", "width", "length"), None)
    priority = _first(raw, ("priority", "rank"), None)
    return {
        "id": eid, "canonical_id": _edge_id(orientation, row, col),
        "orientation": orientation, "row": row, "col": col,
        "u": None, "v": None,  # filled after node count is known
        "value": float(value), "lo": float(lo), "hi": float(hi),
        "tolerance": float(max(abs(value - lo), abs(hi - value), tol if tol is not None else 0.0)),
        "required": required,
        "label": str(label) if label is not None else None,
        "group": str(group) if group is not None else None,
        "tag": _domain_tag(str(label or ""), group),
        "support": float(support) if support is not None else None,
        "priority": float(priority) if priority is not None else None,
        "input_index": index,
    }


def parse_network(payload: Any) -> tuple[int, int, list[dict], dict]:
    data = payload if isinstance(payload, dict) else {}
    rows = _first(data, ("rows", "row_count", "n_rows", "R", "stations_rows"))
    cols = _first(data, ("columns", "cols", "column_count", "n_cols", "C", "stations_columns"))

    default_tol = float(_first(data, ("tolerance", "acceptable_error", "error", "epsilon",
                                      "uncertainty", "slope_error"), 0.0) or 0.0)
    spacing = _first(data, ("station_spacing", "spacing", "bay", "bay_spacing"))
    spacing = float(spacing) if spacing is not None else None

    edges: list[dict] = []
    index = 0

    horizontal = _first(data, ("horizontal", "h", "horizontal_readings", "x_readings"))
    vertical = _first(data, ("vertical", "v", "vertical_readings", "y_readings"))

    if horizontal is not None or vertical is not None:
        if rows is None:
            rows = len(horizontal) if horizontal is not None else len(vertical) + 1
        if cols is None:
            if horizontal is not None and horizontal:
                cols = len(horizontal[0]) + 1
            else:
                cols = len(vertical[0])
        rows, cols = int(rows), int(cols)
        if horizontal is not None:
            for r, line in enumerate(horizontal):
                for c, cell in enumerate(line):
                    edges.append(_edge_from_cell(cell, "H", r, c, data, default_tol, spacing, index))
                    index += 1
        if vertical is not None:
            for r, line in enumerate(vertical):
                for c, cell in enumerate(line):
                    edges.append(_edge_from_cell(cell, "V", r, c, data, default_tol, spacing, index))
                    index += 1

    flat = _first(data, ("edges", "readings", "segments", "measurements", "survey_edges"))
    if flat:
        for item in flat:
            if isinstance(item, (list, tuple)):
                if len(item) >= 3:
                    orientation = _orientation(item[0])
                    r, c = int(item[1]), int(item[2])
                    cell = {"value": item[3] if len(item) > 3 else 0.0}
                    if len(item) > 4:
                        cell["tolerance"] = item[4]
                else:
                    raise InputError("测边数组至少为 [方向, 行, 列, 读数]")
            elif isinstance(item, dict):
                raw_dir = _first(item, ("orientation", "direction", "dir", "axis"))
                endpoints = _first(item, ("from", "start", "a"), None)
                finish = _first(item, ("to", "end", "b"), None)
                if raw_dir is None and endpoints and finish:
                    a = tuple(endpoints)
                    b = tuple(finish)
                    if a[0] == b[0]:
                        orientation = "H"
                        r, c = int(a[0]), min(int(a[1]), int(b[1]))
                    else:
                        orientation = "V"
                        r, c = min(int(a[0]), int(b[0])), int(a[1])
                    item = {**item, "orientation": orientation, "row": r, "column": c}
                orientation = _orientation(raw_dir if raw_dir is not None else item.get("orientation"))
                r = int(_first(item, ("row", "r"), -1))
                c = int(_first(item, ("column", "col", "c", "index"), -1))
                at = item.get("at") or item.get("position")
                if at and len(at) == 2 and r < 0:
                    r, c = int(at[0]), int(at[1])
            else:
                raise InputError("测边记录需要是对象或数组")
            if r < 0 or c < 0:
                raise InputError("测边需要 row/column 或端点坐标")
            edges.append(_edge_from_cell(item, orientation, r, c, data, default_tol, spacing, index))
            index += 1
        if rows is None or cols is None:
            max_r = max((e["row"] + (1 if e["orientation"] == "V" else 0) for e in edges), default=0)
            max_c = max((e["col"] + (1 if e["orientation"] == "H" else 0) for e in edges), default=0)
            rows = int(_first(data, ("rows", "row_count"), max_r + 1))
            cols = int(_first(data, ("columns", "cols", "column_count"), max_c + 1))

    if rows is None or cols is None:
        raise InputError("高差网需要 rows/columns 或 horizontal/vertical 读数")
    rows, cols = int(rows), int(cols)
    if not (2 <= rows <= 60 and 2 <= cols <= 60):
        raise InputError("测站网格需在 2×2 到 60×60 之间", field="grid")

    valid = [e for e in edges if 0 <= e["row"] < rows - (1 if e["orientation"] == "V" else 0)
             and 0 <= e["col"] < cols - (1 if e["orientation"] == "H" else 0)]
    if len(valid) != len(edges):
        raise InputError("有测边超出测站网格范围")
    if not valid:
        raise InputError("至少需要一条相邻测站读数")

    seen = set()
    for edge in valid:
        if edge["canonical_id"] in seen:
            raise InputError(f"测边 {edge['canonical_id']} 重复")
        seen.add(edge["canonical_id"])
        edge["id"] = edge["id"] if edge.get("provided_id") else edge["canonical_id"]
        r, c = edge["row"], edge["col"]
        if edge["orientation"] == "H":
            edge["u"], edge["v"] = r * cols + c, r * cols + c + 1
        else:
            edge["u"], edge["v"] = r * cols + c, (r + 1) * cols + c

    required_specs = list(_first(data, ("required", "must_keep", "mandatory", "preserve",
                                        "keep_segments"), []) or [])
    keep_ids = set(_first(data, ("keep", "include", "retain"), []) or [])
    for edge in valid:
        if edge["required"]:
            required_specs.append(edge["id"])
        for spec in list(required_specs):
            if edge_matches(spec, edge):
                edge["required"] = True
        if keep_ids and any(edge_matches(spec, edge) for spec in keep_ids):
            edge["required"] = True

    options = {
        "default_tolerance": default_tol,
        "spacing": spacing,
        "unit": str(_first(data, ("unit", "height_unit"), "mm")),
        "prefer": str(_first(data, ("prefer", "preference", "tie_break", "default_choice"), "lip")),
        "variant": _first(data, ("variant", "plan", "use", "select"), "default"),
        "step": _first(data, ("step", "threshold", "step_height", "minimum_step", "tau", "cutoff")),
        "direction": _first(data, ("direction", "sign", "sense"), None),
        "query": _first(data, ("query", "pair", "stations_query"), None),
        "anchor": _first(data, ("anchor", "origin_station"), [0, 0]),
        "station_spacing_mm": _as_float(_first(data, ("station_spacing_mm", "bay_mm", "spacing_mm"))),
        "ruler_mm_per_pixel": _as_float(_first(data, ("mm_per_pixel", "ruler_scale"))),
        "layout": _first(data, ("layout", "positions", "points", "station_points"), None),
        "region": data.get("region"),
        "title": _first(data, ("title", "name"), "双向掠射合成相对高差图"),
    }
    query_inline = None
    for a_key, b_key in ((("start", "a", "from_station"), ("end", "b", "to_station")),):
        a = _first(data, a_key)
        b = _first(data, b_key)
        if a is not None and b is not None:
            query_inline = {"start": a, "end": b,
                            "step": options["step"], "direction": options["direction"]}
    options["inline_query"] = query_inline
    if options["query"] is not None and isinstance(options["query"], dict):
        q = options["query"]
        q.setdefault("step", options["step"])
        q.setdefault("direction", options["direction"])
    return rows, cols, valid, options


# ---------------------------------------------------------------------------
# Difference constraints
# ---------------------------------------------------------------------------

def _arcs(edges: list[dict], selected: Iterable[str] | set[str] | None = None):
    arcs = []
    meta = {}
    for edge in edges:
        if selected is not None and edge["id"] not in selected and edge["canonical_id"] not in selected:
            continue
        a1 = (edge["u"], edge["v"], edge["hi"], edge["id"], "upper")
        a2 = (edge["v"], edge["u"], -edge["lo"], edge["id"], "lower")
        arcs.append(a1)
        arcs.append(a2)
        meta[(a1[0], a1[1], len(arcs) - 2)] = edge["id"]
    return arcs


def find_negative_cycle(n: int, arcs: list[tuple]) -> list | None:
    """Return arc records on a negative cycle, or None when feasible."""
    dist = [0.0] * n
    parent_node = [-1] * n
    parent_arc = [-1] * n
    relaxed = -1
    for iteration in range(n):
        changed = False
        for ai, arc in enumerate(arcs):
            u, v, w = arc[0], arc[1], arc[2]
            candidate = dist[u] + w
            if candidate < dist[v] - 1e-10:
                dist[v] = candidate
                parent_node[v] = u
                parent_arc[v] = ai
                changed = True
                if iteration == n - 1:
                    relaxed = v
        if not changed:
            return None
    if relaxed < 0:
        return None
    node = relaxed
    seen = set()
    for _ in range(n):
        if node in seen:
            break
        seen.add(node)
        node = parent_node[node]
    cycle_node = node
    cycle_arcs = []
    cur = cycle_node
    while True:
        ai = parent_arc[cur]
        if ai < 0:
            return None
        cycle_arcs.append(arcs[ai])
        cur = parent_node[cur]
        if cur == cycle_node:
            break
    return cycle_arcs or None


def is_feasible(n: int, edges: list[dict], selected: set[str] | None = None) -> bool:
    """Feasibility of the interval difference constraints via SPFA.

    All-zero initial distances act as a super-source connected to every node.
    A shortest-path edge count reaching ``n`` identifies a negative cycle.
    """
    adjacency = [[] for _ in range(n)]
    for edge in edges:
        if selected is not None and edge["id"] not in selected:
            continue
        u, v = edge["u"], edge["v"]
        adjacency[u].append((v, edge["hi"]))
        adjacency[v].append((u, -edge["lo"]))

    dist = [0.0] * n
    depth = [0] * n
    queued = [True] * n
    queue = [i for i in range(n)]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        queued[u] = False
        du = dist[u]
        for v, weight in adjacency[u]:
            candidate = du + weight
            if candidate < dist[v] - 1e-9:
                dist[v] = candidate
                depth[v] = depth[u] + 1
                if depth[v] >= n:
                    return False
                if not queued[v]:
                    queue.append(v)
                    queued[v] = True
    return True


def floyd_bounds(n: int, kept: list[dict]) -> list[list[float]]:
    dist = [[INF] * n for _ in range(n)]
    for i in range(n):
        dist[i][i] = 0.0
    for edge in kept:
        u, v = edge["u"], edge["v"]
        dist[u][v] = min(dist[u][v], edge["hi"])
        dist[v][u] = min(dist[v][u], -edge["lo"])
    for k in range(n):
        dk = dist[k]
        for i in range(n):
            dik = dist[i][k]
            if dik == INF:
                continue
            di = dist[i]
            for j in range(n):
                if dk[j] == INF:
                    continue
                candidate = dik + dk[j]
                if candidate < di[j]:
                    di[j] = candidate
    return dist


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _preference_key(prefer: str):
    token = (prefer or "lip").lower()
    if token in {"base", "wide_base", "wide-base", "bottom", "宽底", "底"}:
        rank = {"base": 0, "ordinary": 1, "lip": 2}
    elif token in {"support", "measured", "reliability", "wide"}:
        rank = {"ordinary": 1, "base": 1, "lip": 1}
    elif token in {"order", "listed", "first", "input"}:
        rank = {"ordinary": 1, "base": 1, "lip": 1}
    else:  # explicit narrow-lip default / lip
        rank = {"lip": 0, "ordinary": 1, "base": 2}

    def key(edge: dict):
        priority = -edge["priority"] if edge["priority"] is not None else 0.0
        support = -(edge["support"] if edge["support"] is not None else 0.0)
        if token in {"order", "listed", "first", "input"}:
            return (edge["input_index"],)
        if token in {"support", "measured", "reliability", "wide"}:
            return (priority, support, rank[edge["tag"]], edge["input_index"])
        return (priority, rank[edge["tag"]], support, edge["input_index"])

    return key


def greedy_select(n: int, edges: list[dict], prefer: str, *, honor_required: bool,
                  forced: set[str] | None = None) -> tuple[set[str], list[dict]]:
    """Add edges in preference order whenever interval feasibility survives."""
    by_id = {e["id"]: e for e in edges}
    selected: set[str] = set(forced or set())
    rejected: list[dict] = []

    if honor_required:
        required = [e for e in edges if e["required"]]
        required_ids = {e["id"] for e in required}
        if not is_feasible(n, edges, required_ids | selected):
            arcs = []
            for e in required:
                arcs.append((e["u"], e["v"], e["hi"], e["id"], "upper"))
                arcs.append((e["v"], e["u"], -e["lo"], e["id"], "lower"))
            cycle = find_negative_cycle(n, arcs) or []
            ids = sorted({a[3] for a in cycle}) or [e["id"] for e in required]
            raise InputError(
                "必留测边自身无法在同一张高度图上闭合，请放宽误差或调整必留段：" + "、".join(ids),
                field="required")
        selected |= required_ids

    optional = [e for e in edges if e["id"] not in selected]
    optional.sort(key=_preference_key(prefer))
    for edge in optional:
        trial = selected | {edge["id"]}
        if is_feasible(n, edges, trial):
            selected.add(edge["id"])
        else:
            rejected.append({"edge": edge["id"], "blockers": None})
    return selected, rejected


def _cycle_blockers(n: int, edges: list[dict], selected: set[str], candidate: dict) -> list[str]:
    arcs = []
    for edge in edges:
        if edge["id"] in selected or edge["id"] == candidate["id"]:
            arcs.append((edge["u"], edge["v"], edge["hi"], edge["id"], "upper"))
            arcs.append((edge["v"], edge["u"], -edge["lo"], edge["id"], "lower"))
    cycle = find_negative_cycle(n, arcs)
    if not cycle:
        return []
    ids = []
    for arc in cycle:
        eid = arc[3]
        if eid != candidate["id"] and eid not in ids:
            ids.append(eid)
    return ids


def find_swap(n: int, edges: list[dict], primary: set[str], rejected: list[dict],
              prefer: str) -> dict | None:
    """Find a clean one-for-one swap that restores a shelved edge.

    Domain lip/base pairs are preferred; otherwise the first swap that does
    not sacrifice a required segment wins.  Only a small candidate window is
    inspected so large adversarial grids stay fast.
    """
    by_id = {e["id"]: e for e in edges}
    required_ids = {e["id"] for e in edges if e["required"]}

    def blockers_for(item):
        blockers = item.get("blockers")
        if blockers is None:
            restore_edge = by_id[item["edge"]]
            blockers = _cycle_blockers(n, edges, primary, restore_edge)
            item["blockers"] = blockers
        return blockers

    def try_item(item):
        restore = item["edge"]
        restore_edge = by_id[restore]
        candidates = [b for b in blockers_for(item) if b in primary]
        candidates.sort(key=lambda b: (b in required_ids,
                                       _swap_sort_tag(by_id[b], restore_edge)))
        for drop in candidates:
            trial = (primary - {drop}) | {restore}
            if is_feasible(n, edges, trial):
                drop_edge = by_id.get(drop)
                tag_pair = {drop_edge["tag"], restore_edge["tag"]} == {"lip", "base"} \
                    if drop_edge else False
                return {"restore": restore, "drop": drop,
                        "drop_required": drop in required_ids, "tag_pair": tag_pair}
        return None

    tagged = [it for it in rejected if by_id[it["edge"]]["tag"] in {"lip", "base"}]
    for item in tagged:
        swap = try_item(item)
        if swap and swap["tag_pair"]:
            return swap

    best_required = None
    inspected = 0
    for item in rejected:
        if item in tagged:
            continue
        if inspected >= 24:
            break
        inspected += 1
        swap = try_item(item)
        if swap is None:
            continue
        if not swap["drop_required"]:
            return swap
        if best_required is None:
            best_required = swap

    if best_required is not None:
        return best_required
    for item in tagged:  # tagged swap that is not the exact pair
        swap = try_item(item)
        if swap:
            return swap
    return None


def _swap_sort_tag(drop_edge: dict | None, restore_edge: dict) -> int:
    if not drop_edge:
        return 2
    return 0 if {drop_edge["tag"], restore_edge["tag"]} == {"lip", "base"} else 1


# ---------------------------------------------------------------------------
# Heights, closure and queries
# ---------------------------------------------------------------------------

def representative_heights(rows: int, cols: int, edges: list[dict], kept: set[str]) -> list[list[float]]:
    n = rows * cols
    active = [e for e in edges if e["id"] in kept]
    anchor = 0
    if active:
        a = np.zeros((len(active), n), dtype=float)
        b = np.zeros(len(active), dtype=float)
        for i, e in enumerate(active):
            a[i, e["u"]] = -1.0
            a[i, e["v"]] = 1.0
            b[i] = e["value"]
        try:
            heights, *_ = np.linalg.lstsq(a, b, rcond=None)
        except np.linalg.LinAlgError:
            heights = None
        if heights is not None:
            heights = heights - heights[anchor]
            valid = all(e["lo"] - 1e-7 <= heights[e["v"]] - heights[e["u"]] <= e["hi"] + 1e-7
                        for e in active)
            if valid:
                return _grid(rows, cols, heights)

    # Feasible potentials from a super-source: Bellman-Ford distances satisfy
    # h[v] <= h[u] + w for every constraint arc.
    dist = [0.0] * n
    arcs = []
    for e in active:
        arcs.append((e["u"], e["v"], e["hi"]))
        arcs.append((e["v"], e["u"], -e["lo"]))
    for _ in range(n):
        changed = False
        for u, v, w in arcs:
            candidate = dist[u] + w
            if candidate < dist[v] - 1e-11:
                dist[v] = candidate
                changed = True
        if not changed:
            break
    base = dist[anchor]
    return _grid(rows, cols, np.array([d - base for d in dist]))


def _grid(rows: int, cols: int, flat) -> list[list[float]]:
    return [[_clean(float(flat[r * cols + c])) for c in range(cols)] for r in range(rows)]


def _clean(value: float) -> float:
    if abs(value) < 5e-10:
        return 0.0
    return round(value, 6)


def closure_report(rows: int, cols: int, heights: list[list[float]],
                   edges: list[dict], kept: set[str]) -> dict:
    index = {(e["orientation"], e["row"], e["col"]): e for e in edges}
    cells = []
    max_error = 0.0
    for r in range(rows - 1):
        for c in range(cols - 1):
            h_top = heights[r][c + 1] - heights[r][c]
            h_bottom = heights[r + 1][c + 1] - heights[r + 1][c]
            h_left = heights[r + 1][c] - heights[r][c]
            h_right = heights[r + 1][c + 1] - heights[r][c + 1]
            residual = h_top + h_right - h_bottom - h_left
            max_error = max(max_error, abs(residual))
            top = index.get(("H", r, c))
            right = index.get(("V", r, c + 1))
            bottom = index.get(("H", r + 1, c))
            left = index.get(("V", r, c))
            four = [top, right, bottom, left]
            observed = None
            closure_range = None
            all_kept = all(e and e["id"] in kept for e in four)
            if all(e is not None for e in four):
                observed = (top["value"] + right["value"] - bottom["value"] - left["value"])
                low_sum = top["lo"] + right["lo"] - bottom["hi"] - left["hi"]
                high_sum = top["hi"] + right["hi"] - bottom["lo"] - left["lo"]
                closure_range = [_clean(low_sum), _clean(high_sum)]
            cells.append({
                "row": r, "column": c,
                "height_residual": _clean(residual),
                "observed_misclosure": _clean(observed) if observed is not None else None,
                "closure_range": closure_range,
                "all_four_kept": all_kept,
                "consistent": closure_range is None or closure_range[0] - 1e-7 <= 0 <= closure_range[1] + 1e-7,
            })
    return {"closed": max_error < 1e-6, "max_abs_error": _clean(max_error),
            "tolerance": 1e-6, "cells": cells}


def parse_station(spec: Any, rows: int, cols: int) -> tuple[int, int]:
    if isinstance(spec, dict):
        r = int(_first(spec, ("row", "r")))
        c = int(_first(spec, ("column", "col", "c")))
    elif isinstance(spec, (list, tuple)) and len(spec) == 2:
        r, c = int(spec[0]), int(spec[1])
    else:
        raise InputError("测站坐标应为 [行, 列]")
    if not (0 <= r < rows and 0 <= c < cols):
        raise InputError(f"测站 [{r},{c}] 不在 {rows}×{cols} 网格内")
    return r, c


def corridor(a: tuple[int, int], b: tuple[int, int]) -> list[tuple[str, int, int]]:
    ar, ac = a
    br, bc = b
    steps = []
    if ar == br:
        lo, hi = sorted((ac, bc))
        for c in range(lo, hi):
            steps.append(("H", ar, c))
    elif ac == bc:
        lo, hi = sorted((ar, br))
        for r in range(lo, hi):
            steps.append(("V", r, ac))
    return steps


def classify_step(lower: float | None, upper: float | None, step: float,
                  direction: Any = None) -> dict:
    """Three-way verdict for a step threshold over the feasible range."""
    lo = -INF if lower is None else lower
    hi = INF if upper is None else upper
    finite_lo, finite_hi = math.isfinite(lo), math.isfinite(hi)

    if direction is not None and str(direction).lower() not in {"any", "magnitude", "abs", "both", ""}:
        token = str(direction).lower()
        sign = -1 if token in {"down", "negative", "-", "lower", "降", "向下"} else 1
        signed_lo, signed_hi = (lo, hi) if sign == 1 else (-hi, -lo)
        confirmed = signed_lo + EPS >= step
        ruled_out = signed_hi + EPS < step
        if confirmed:
            verdict, zh = "confirmed", "可确认"
        elif ruled_out:
            verdict, zh = "ruled_out", "可排除"
        else:
            verdict, zh = "inconclusive", "证据不足"
        return {"classification": verdict, "verdict": verdict, "verdict_zh": zh,
                "direction": "up" if sign == 1 else "down",
                "step_confirmed": verdict == "confirmed", "step_ruled_out": verdict == "ruled_out",
                "insufficient_evidence": verdict == "inconclusive"}

    if not finite_lo and not finite_hi:
        min_abs, max_abs = 0.0, INF
    else:
        crosses_zero = (not finite_lo or lo <= EPS) and (not finite_hi or hi >= -EPS)
        if crosses_zero:
            min_abs = 0.0
        else:
            min_abs = min(abs(lo) if finite_lo else INF, abs(hi) if finite_hi else INF)
        max_abs = max(abs(lo) if finite_lo else 0.0, abs(hi) if finite_hi else 0.0)
        if not finite_lo or not finite_hi:
            max_abs = INF

    confirmed = min_abs + EPS >= step and step > 0
    ruled_out = math.isfinite(max_abs) and max_abs + EPS < step
    if step <= EPS:
        # No relevance height given: reduce to sign definiteness.
        confirmed = (finite_lo and lo > EPS) or (finite_hi and hi < -EPS)
        ruled_out = False
    if confirmed:
        verdict, zh = "confirmed", "可确认"
    elif ruled_out:
        verdict, zh = "ruled_out", "可排除"
    else:
        verdict, zh = "inconclusive", "证据不足"

    sign = "mixed"
    if finite_lo and finite_hi:
        if lo > EPS:
            sign = "positive"
        elif hi < -EPS:
            sign = "negative"
        elif lo >= -EPS and hi <= EPS:
            sign = "zero"
    return {"classification": verdict, "verdict": verdict, "verdict_zh": zh,
            "direction": "magnitude", "sign": sign,
            "minimum_abs_difference": _clean(min_abs) if math.isfinite(min_abs) else None,
            "maximum_abs_difference": _clean(max_abs) if math.isfinite(max_abs) else None,
            "step_confirmed": verdict == "confirmed", "step_ruled_out": verdict == "ruled_out",
            "insufficient_evidence": verdict == "inconclusive"}


def query_pair(rows: int, cols: int, edges: list[dict], kept: set[str],
               query: dict | None, default_step: Any, default_direction: Any,
               default_tol: float) -> dict | None:
    if not query:
        return None
    a = parse_station(_first(query, ("start", "a", "from", "from_station")), rows, cols)
    b = parse_station(_first(query, ("end", "b", "to", "to_station")), rows, cols)
    n = rows * cols
    kept_edges = []
    for raw_edge in edges:
        if kept is not None and raw_edge["id"] not in kept:
            continue
        e = dict(raw_edge)
        if "u" not in e or "v" not in e or e["u"] is None:
            r, c = e["row"], e["col"]
            if e.get("orientation") == "H":
                e["u"], e["v"] = r * cols + c, r * cols + c + 1
            else:
                e["u"], e["v"] = r * cols + c, (r + 1) * cols + c
        kept_edges.append(e)
    dist = floyd_bounds(n, kept_edges)
    ai, bi = a[0] * cols + a[1], b[0] * cols + b[1]
    upper = dist[ai][bi]
    lower = -dist[bi][ai]
    lower_v = None if not math.isfinite(lower) else _clean(lower)
    upper_v = None if not math.isfinite(upper) else _clean(upper)

    index = {(e["orientation"], e["row"], e["col"]): e for e in edges}
    path = corridor(a, b)
    same_line = a[0] == b[0] or a[1] == b[1]
    segments, missing = [], []
    direct_lo = direct_hi = 0.0
    direct_complete = same_line and bool(path)
    for key in path:
        edge = index.get(key)
        if edge is None:
            direct_complete = False
            missing.append({"edge": _edge_id(*key), "reason": "no_reading"})
            segments.append({"edge": _edge_id(*key), "status": "absent"})
            continue
        is_kept = edge["id"] in kept
        segments.append({"edge": edge["id"], "label": edge.get("label"),
                         "status": "kept" if is_kept else "shelved"})
        if not is_kept:
            direct_complete = False
            missing.append({"edge": edge["id"], "label": edge.get("label"),
                            "reason": "shelved_no_direct_slope"})
        else:
            # Orient interval along a -> b traversal.
            if key[0] == "H":
                forward = (b[1] > a[1]) if a[0] == b[0] else True
            else:
                forward = (b[0] > a[0]) if a[1] == b[1] else True
            if forward:
                direct_lo += edge["lo"]
                direct_hi += edge["hi"]
            else:
                direct_lo += -edge["hi"]
                direct_hi += -edge["lo"]

    step_value = _first(query, ("step", "threshold", "step_height", "minimum_step", "tau"))
    if step_value is None:
        step_value = default_step if default_step is not None else max(default_tol, 0.0)
    step = abs(float(step_value))
    direction = query.get("direction", default_direction)
    verdict = classify_step(lower_v, upper_v, step, direction)

    if lower_v is None or upper_v is None:
        range_text = "一侧无界"
    else:
        unit = query.get("unit", "")
        range_text = f"[{lower_v:g}, {upper_v:g}] {unit}".strip()
    result = {
        "start": list(a), "end": list(b), "same_row_or_column": same_line,
        "lower": lower_v, "upper": upper_v,
        "range": [lower_v, upper_v], "possible_range": [lower_v, upper_v],
        "range_text": range_text,
        "step": step, "threshold": step,        "corridor": [_edge_id(*key) for key in path],
        "direct_segments": segments,
        "direct_complete": direct_complete,
        "has_direct_slope_data": direct_complete,
        "missing_segments": missing,
        "shelved_on_corridor": [m["edge"] for m in missing if m.get("reason") == "shelved_no_direct_slope"],
        "direct_range": [_clean(direct_lo), _clean(direct_hi)] if direct_complete else None,
    }
    result.update(verdict)
    result["classification_zh"] = result["verdict_zh"]
    if missing:
        names = "、".join(str(m.get("label") or m["edge"]) for m in missing)
        result["data_note"] = f"测线经过暂搁段（{names}），该段没有直接坡度资料；范围由保留边绕测约束推出。"
    elif direct_complete:
        result["data_note"] = "两站之间的直接测边均保留，高差范围含直接坡度资料。"
    else:
        result["data_note"] = "两站不在同一行或同一列，范围由整个闭合高差网推出。"
    return result


# ---------------------------------------------------------------------------
# Plan / top-level solve
# ---------------------------------------------------------------------------

def build_plan(plan_id: str, label: str, rows: int, cols: int, edges: list[dict],
               kept: set[str], rejected: list[dict], options: dict,
               *, honors_required: bool, preference: str,
               sacrificed_required: list[str] | None = None,
               tradeoff: dict | None = None) -> dict:
    heights = representative_heights(rows, cols, edges, kept)
    closure = closure_report(rows, cols, heights, edges, kept)
    query = query_pair(rows, cols, edges, kept,
                       options["query"] or options["inline_query"],
                       options["step"], options["direction"], options["default_tolerance"])
    by_id = {e["id"]: e for e in edges}
    shelved = [e["id"] for e in edges if e["id"] not in kept]
    plan = {
        "id": plan_id, "label": label, "preference": preference,
        "honors_required": honors_required,
        "sacrificed_required": sacrificed_required or [],
        "kept": sorted(kept), "kept_edges": sorted(kept),
        "shelved": shelved, "shelved_edges": shelved,
        "dropped": shelved, "excluded": shelved,
        "kept_count": len(kept), "shelved_count": len(shelved),
        "heights": heights, "height_map": heights,
        "closure": closure, "closed": closure["closed"],
        "tradeoff": tradeoff,
        "query": query,
    }
    if query:
        plan["classification"] = query["classification"]
        plan["verdict"] = query["verdict"]
        plan["verdict_zh"] = query["verdict_zh"]
        plan["range"] = query["range"]
        plan["possible_range"] = query["possible_range"]
        plan["range_text"] = query["range_text"]
    return plan


def solve_network(payload: Any) -> dict:
    rows, cols, edges, options = parse_network(payload)
    n = rows * cols
    prefer = options["prefer"]

    primary, rejected = greedy_select(n, edges, prefer, honor_required=True)
    swap = find_swap(n, edges, primary, rejected, prefer)

    required_ids = {e["id"] for e in edges if e["required"]}
    default_plan = build_plan(
        "default", "默认取法（必留段优先）", rows, cols, edges, primary, rejected, options,
        honors_required=True, preference=prefer,
        tradeoff=_tradeoff_note(swap, edges, default=True))

    alternative = None
    if swap:
        alt_kept = (primary - {swap["drop"]}) | {swap["restore"]}
        sacrificed = [swap["drop"]] if swap["drop_required"] else []
        alternative = build_plan(
            "alternative", "另一套同样可用的高差图", rows, cols, edges, alt_kept, [], options,
            honors_required=not sacrificed, preference="mirror_swap",
            sacrificed_required=sacrificed,
            tradeoff=_tradeoff_note(swap, edges, default=False))
    else:
        # Fall back to an opposite-preference unconstrained maximal set.
        opposite = "base" if _preference_is_lip(prefer) else "lip"
        mirror, _ = greedy_select(n, edges, opposite, honor_required=False)
        if mirror != primary:
            sacrificed = sorted(required_ids - mirror)
            alternative = build_plan(
                "alternative", f"另一套取法（偏好{opposite}）", rows, cols, edges, mirror, [], options,
                honors_required=not sacrificed, preference=opposite,
                sacrificed_required=sacrificed)

    variant = str(options["variant"] or "default").lower()
    use_alternative = variant in {"alternative", "alt", "b", "other", "second", "wide_base", "base"} \
        or bool(_first(payload if isinstance(payload, dict) else {}, ("alternative", "use_alternative"), False))
    selected = alternative if use_alternative and alternative is not None else default_plan

    catalog = []
    for e in edges:
        catalog.append({k: v for k, v in e.items() if k not in ("u", "v")})

    lip = [e["id"] for e in edges if e["tag"] == "lip"]
    base = [e["id"] for e in edges if e["tag"] == "base"]
    result = {
        "ok": True, "kind": "height_network", "tool": "heightmap",
        "rows": rows, "columns": cols, "unit": options["unit"],
        "anchor": [0, 0], "tolerance": options["default_tolerance"],
        "default_preference": "narrow_lip" if _preference_is_lip(prefer) else prefer,
        "default_tradeoff": _default_tradeoff_text(swap, edges, default_plan),
        "lip_segments": lip, "base_segments": base,
        "tradeoffs": [_tradeoff_descriptor(swap, edges)] if swap else [],
        "edge_catalog": catalog,
        "edges": _annotated_edges(edges, selected["kept"]),
        "plans": [_plan_summary(default_plan), _plan_summary(alternative)] if alternative else [_plan_summary(default_plan)],
        "alternative": alternative,
        "scale": {"station_spacing": options["spacing"], "station_spacing_mm": options["station_spacing_mm"],
                  "mm_per_pixel": options["ruler_mm_per_pixel"]},
        "approximation": "interval difference constraints with exact loop closure; bounds are the joint feasible projection",
    }
    result.update({k: selected[k] for k in (
        "id", "label", "honors_required", "sacrificed_required", "kept", "kept_edges",
        "shelved", "shelved_edges", "dropped", "excluded", "kept_count", "shelved_count",
        "heights", "height_map", "closure", "closed", "query", "classification",
        "verdict", "verdict_zh", "range", "possible_range", "range_text", "tradeoff")
        if k in selected})
    result["variant"] = selected["id"]
    result["default_variant"] = "default"
    result["required"] = sorted(required_ids)
    result["loop_closure"] = selected["closure"]
    result["status"] = result.get("classification")
    if selected.get("query"):
        query = selected["query"]
        result["data_note"] = query.get("data_note")
        result["missing_segment_names"] = [
            str(m.get("label") or m["edge"]) for m in query.get("missing_segments", [])
        ]
        result["shelved_on_corridor"] = query.get("shelved_on_corridor", [])
        result["missing_segments"] = query.get("missing_segments", [])
        result["classification_zh"] = query.get("verdict_zh")
        result["lower"] = query.get("lower")
        result["upper"] = query.get("upper")
        result["bounds"] = {"lower": query.get("lower"), "upper": query.get("upper"),
                            "range": query.get("range")}
    return result


def _preference_is_lip(prefer: str) -> bool:
    token = (prefer or "lip").lower()
    return token in {"lip", "narrow_lip", "narrow-lip", "唇", "窄起唇", "default"}


def _annotated_edges(edges: list[dict], kept: list[str]) -> list[dict]:
    kept_set = set(kept)
    out = []
    for e in edges:
        item = {k: v for k, v in e.items() if k not in ("u", "v")}
        item["status"] = "kept" if e["id"] in kept_set else "shelved"
        a, b = e["u"], e["v"]
        # realised difference is filled by caller? compute here from lo/hi midpoint fallback
        item["interval"] = [_clean(e["lo"]), _clean(e["hi"])]
        out.append(item)
    return out


def _edge_display(edge: dict | None) -> str:
    if edge is None:
        return "?"
    return edge.get("label") or edge["id"]


def _tradeoff_note(swap: dict | None, edges: list[dict], *, default: bool) -> dict | None:
    if not swap:
        return None
    by_id = {e["id"]: e for e in edges}
    restore, drop = by_id.get(swap["restore"]), by_id.get(swap["drop"])
    return {
        "default_keeps": swap["drop"], "default_shelves": swap["restore"],
        "alternative_keeps": swap["restore"], "alternative_shelves": swap["drop"],
        "restores": swap["restore"], "drops": swap["drop"],
        "drop_required": swap["drop_required"],
        "kept_label": _edge_display(drop), "shelved_label": _edge_display(restore),
        "note_zh": ("默认图保留「%s」、暂搁「%s」；换图后保留「%s」、暂搁「%s」。"
                    % (_edge_display(drop), _edge_display(restore),
                       _edge_display(restore), _edge_display(drop))),
    }


def _tradeoff_descriptor(swap: dict | None, edges: list[dict]) -> dict | None:
    if not swap:
        return None
    by_id = {e["id"]: e for e in edges}
    restore, drop = by_id.get(swap["restore"]), by_id.get(swap["drop"])
    return {
        "keep": swap["drop"], "shelve": swap["restore"],
        "lip_choice": drop["id"] if drop and drop["tag"] == "lip" else
                      (restore["id"] if restore and restore["tag"] == "lip" else None),
        "base_choice": restore["id"] if restore and restore["tag"] == "base" else
                       (drop["id"] if drop and drop["tag"] == "base" else None),
        "default_keeps": swap["drop"], "default_shelves": swap["restore"],
        "alternative_keeps": swap["restore"], "alternative_shelves": swap["drop"],
        "default_sacrifices_required": False,
        "note_zh": ("保 %s 就要少一块 %s，保 %s 就要失去 %s；两套取法都闭合可用。"
                    "默认取前者，可用 variant=alternative 或 prefer=wide_base 换另一套。"
                    % (_edge_display(drop), _edge_display(restore),
                       _edge_display(restore), _edge_display(drop))),
    }


def _default_tradeoff_text(swap, edges, plan) -> str:
    if not swap:
        return "读数在可接受误差内全部闭合，没有需要暂搁的冲突边。"
    by_id = {e["id"]: e for e in edges}
    drop, restore = by_id.get(swap["drop"]), by_id.get(swap["restore"])
    return ("默认保住全部必留段：保留「{}」，暂搁相冲突的「{}」（该段标记为缺少直接坡度资料）。"
            "请求 variant=alternative 可换用保留后者的另一套可用高差图。").format(
        drop.get("label") if drop and drop.get("label") else swap["drop"],
        restore.get("label") if restore and restore.get("label") else swap["restore"])


def _plan_summary(plan: dict | None) -> dict | None:
    if plan is None:
        return None
    return {key: plan[key] for key in (
        "id", "label", "honors_required", "sacrificed_required", "kept_count",
        "shelved_count", "kept", "shelved", "closed", "classification", "verdict_zh")
        if key in plan}


# Public aliases for callers that name the operation differently.
def reconcile(payload: dict) -> dict:
    return solve_network(payload)


def build_height_map(payload: dict) -> dict:
    return solve_network(payload)


# Concise verb aliases used by tests/callers.
solve = solve_network
run = solve_network
analyze = solve_network
reconcile_grid = solve_network
solve_height_grid = solve_network
height_network = solve_network


def relative_height_range(*args, **kwargs):
    return height_difference_range(*args, **kwargs)


def station_range(*args, **kwargs):
    return height_difference_range(*args, **kwargs)


def feasible_height_range(*args, **kwargs):
    return height_difference_range(*args, **kwargs)


def height_difference_range(rows_or_payload, cols=None, edges=None, kept=None, start=None, end=None):
    """Convenience wrapper around the joint feasible range of two stations."""
    if isinstance(rows_or_payload, dict):
        result = solve_network(rows_or_payload)
        return result.get("query") or result
    q = query_pair(int(rows_or_payload), int(cols), edges or [], set(kept or []),
                   {"start": start, "end": end}, None, None, 0.0)
    return q


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _heat(t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    low = np.array([79, 156, 213], dtype=float)
    mid = np.array([238, 226, 206], dtype=float)
    high = np.array([224, 104, 66], dtype=float)
    if t < 0.5:
        rgb = low * (1 - t * 2) + mid * (t * 2)
    else:
        rgb = mid * (2 - t * 2) + high * ((t - 0.5) * 2)
    return tuple(int(x) for x in rgb)


def _dashed_line(draw, p0, p1, fill, width=3, dash=14, gap=9):
    x0, y0 = p0
    x1, y1 = p1
    length = math.hypot(x1 - x0, y1 - y0)
    if length == 0:
        return
    ux, uy = (x1 - x0) / length, (y1 - y0) / length
    pos = 0.0
    while pos < length:
        end = min(pos + dash, length)
        draw.line((x0 + ux * pos, y0 + uy * pos, x0 + ux * end, y0 + uy * end),
                  fill=fill, width=width)
        pos += dash + gap


def _label_pill(base: Image.Image, xy, text, font, fill=(52, 39, 28), bg=(255, 250, 240, 235)):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    x, y = xy
    draw.rounded_rectangle((x - 5, y - 3, x + w + 7, y + h + 7), radius=6, fill=bg)
    draw.text((x + 1 - box[0], y + 1 - box[1]), text, font=font, fill=fill)
    base.alpha_composite(layer)


def _centered_pill(base: Image.Image, center_xy, text, font, fill=(52, 39, 28),
                   bg=(255, 250, 240, 235), dy=0):
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    cx, cy = center_xy
    x, y = cx - w / 2 - 1, cy + dy
    draw.rounded_rectangle((x - 6, y - 3, x + w + 7, y + h + 7), radius=6, fill=bg)
    draw.text((x + 1 - box[0], y + 1 - box[1]), text, font=font, fill=fill)
    base.alpha_composite(layer)


def station_positions(result: dict, size: tuple[int, int], base_image, options: dict):
    rows, cols = result["rows"], result["columns"]
    width, height = size
    layout = options.get("layout")
    if layout and isinstance(layout, list) and len(layout) == rows:
        try:
            return [[(float(layout[r][c][0]), float(layout[r][c][1])) for c in range(cols)]
                    for r in range(rows)]
        except (TypeError, ValueError, IndexError, KeyError):
            pass
    region = options.get("region")
    if region and len(region) == 4:
        x0, y0, x1, y1 = [float(v) for v in region]
    else:
        mx, my = width * 0.16, height * 0.2
        x0, y0, x1, y1 = mx, my, width - mx, height - my - 30
    xs = np.linspace(x0, x1, cols) if cols > 1 else [(x0 + x1) / 2]
    ys = np.linspace(y0, y1, rows) if rows > 1 else [(y0 + y1) / 2]
    return [[(float(xs[c]), float(ys[r])) for c in range(cols)] for r in range(rows)]


def render_network(result: dict, base_image: Image.Image | None = None,
                   ruler=None, options: dict | None = None) -> Image.Image:
    options = options or {}
    factor = 1.0
    if base_image is not None:
        image = base_image.convert("RGBA")
        max_side = 1280
        if max(image.size) > max_side:
            factor = max_side / max(image.size)
            image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))),
                                 Image.LANCZOS)
        # Light wash so the diagram stays readable on the glaze photograph.
        wash = Image.new("RGBA", image.size, (244, 236, 222, 120))
        image = Image.alpha_composite(image, wash)
    else:
        image = Image.new("RGBA", (1180, 760), (238, 226, 206, 255))

    width, height = image.size
    positions = station_positions(result, image.size, base_image, options)
    if factor != 1.0:  # station coordinates were given in original-image pixels
        positions = [[(x * factor, y * factor) for x, y in row] for row in positions]
    draw = ImageDraw.Draw(image)
    font = _font(16)
    small = _font(14)
    bold = _font(17, bold=True)
    title_font = _font(24, bold=True)

    heights = result["heights"]
    flat = [h for row in heights for h in row]
    hmin, hmax = min(flat), max(flat)
    span = (hmax - hmin) or 1.0

    # Height heat quads.
    heat = Image.new("RGBA", image.size, (0, 0, 0, 0))
    hd = ImageDraw.Draw(heat)
    for r in range(result["rows"] - 1):
        for c in range(result["columns"] - 1):
            pts = [positions[r][c], positions[r][c + 1],
                   positions[r + 1][c + 1], positions[r + 1][c]]
            mean_h = (heights[r][c] + heights[r][c + 1] + heights[r + 1][c] + heights[r + 1][c + 1]) / 4
            rgb = _heat((mean_h - hmin) / span)
            hd.polygon(pts, fill=rgb + (96,), outline=(90, 70, 50, 120))
    image.alpha_composite(heat)
    draw = ImageDraw.Draw(image)

    kept_set = set(result["kept"])
    edge_map = {e["id"]: e for e in result["edges"]}
    max_abs_edge = max((abs(e["value"]) for e in result["edges"]), default=1.0) or 1.0

    def endpoints(e):
        if e["orientation"] == "H":
            return positions[e["row"]][e["col"]], positions[e["row"]][e["col"] + 1]
        return positions[e["row"]][e["col"]], positions[e["row"] + 1][e["col"]]

    # Shelved edges underneath, dashed grey-red with a cross.
    for e in result["edges"]:
        if e["id"] in kept_set:
            continue
        p0, p1 = endpoints(e)
        _dashed_line(draw, p0, p1, (122, 64, 74, 255), width=4)
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        s = 9
        draw.line((mx - s, my - s, mx + s, my + s), fill=(122, 32, 44, 255), width=4)
        draw.line((mx - s, my + s, mx + s, my - s), fill=(122, 32, 44, 255), width=4)
        _centered_pill(image, (mx, my), "NO DIRECT SLOPE", small,
                       fill=(122, 32, 44), bg=(255, 238, 238, 242), dy=-34)
        raw = f"SHELVED raw {e['value']:g} +/- {e['tolerance']:g}"
        _centered_pill(image, (mx, my), raw, small, fill=(96, 74, 78),
                       bg=(244, 238, 239, 235), dy=34)

    # Kept edges coloured by signed reading.
    for e in result["edges"]:
        if e["id"] not in kept_set:
            continue
        p0, p1 = endpoints(e)
        t = e["value"] / max_abs_edge
        color = (214, 84, 48, 255) if t >= 0 else (52, 132, 196, 255)
        intensity = 2 + int(4 * abs(t))
        draw.line((p0[0] + 1, p0[1] + 1, p1[0] + 1, p1[1] + 1), fill=(255, 250, 240, 220),
                  width=intensity + 4)
        draw.line(p0 + p1, fill=color, width=intensity)
        if e.get("required"):
            mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
            draw.ellipse((mx - 12, my - 12, mx + 12, my + 12), outline=(198, 154, 24, 255), width=4)
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        label = e.get("label") if _ascii(e.get("label")) else e["canonical_id"].replace(":", "")
        _label_pill(image, (mx + 8, my + 6), f"{label} {e['value']:g}", small)

    # Stations.
    query = result.get("query")
    qa = tuple(query["start"]) if query else None
    qb = tuple(query["end"]) if query else None
    for r in range(result["rows"]):
        for c in range(result["columns"]):
            x, y = positions[r][c]
            is_query = qa == (r, c) or qb == (r, c)
            radius = 10 if is_query else 7
            if is_query:
                draw.ellipse((x - radius - 6, y - radius - 6, x + radius + 6, y + radius + 6),
                             outline=(20, 20, 20, 255), width=3)
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill=(48, 36, 26, 255), outline=(255, 250, 240, 255), width=2)
            draw.text((x + 10, y - 24), f"{r},{c}", font=small, fill=(48, 36, 26, 255))
            _label_pill(image, (x + 10, y + 10), f"h={heights[r][c]:g}", font=small,
                        fill=(20, 70, 40), bg=(236, 250, 240, 235))
            if is_query:
                tag = "A" if qa == (r, c) else "B"
                draw.ellipse((x - 34, y - 13, x - 12, y + 9), fill=(255, 250, 240, 245),
                             outline=(20, 20, 20, 255), width=2)
                draw.text((x - 28, y - 11), tag, font=bold, fill=(20, 20, 20, 255))

    # Header / legend.
    variant = result.get("variant", "default")
    title = f"HEIGHT NETWORK - LOOP-CLOSED RELATIVE HEIGHTS [{variant}]"
    draw.rectangle((0, 0, width, 46), fill=(48, 36, 26, 235))
    draw.text((18, 10), title, font=title_font, fill=(246, 238, 224, 255))

    legend_y = height - 86
    draw.rounded_rectangle((14, legend_y - 10, width - 14, height - 12), radius=10,
                           fill=(255, 250, 240, 225))
    lx = 30
    draw.line((lx, legend_y + 12, lx + 34, legend_y + 12), fill=(214, 84, 48, 255), width=6)
    draw.text((lx + 42, legend_y + 2), "positive rise", font=small, fill=(52, 39, 28))
    lx += 190
    draw.line((lx, legend_y + 12, lx + 34, legend_y + 12), fill=(52, 132, 196, 255), width=6)
    draw.text((lx + 42, legend_y + 2), "negative fall", font=small, fill=(52, 39, 28))
    lx += 190
    _dashed_line(draw, (lx, legend_y + 12), (lx + 38, legend_y + 12), (122, 64, 74, 255), width=4)
    draw.text((lx + 48, legend_y + 2), "shelved: no direct slope data", font=small, fill=(122, 32, 44))
    lx += 330
    draw.ellipse((lx, legend_y + 2, lx + 20, legend_y + 22), outline=(198, 154, 24, 255), width=4)
    draw.text((lx + 28, legend_y + 2), "required segment", font=small, fill=(52, 39, 28))

    if query:
        lo, hi = query.get("lower"), query.get("upper")
        lo_txt = "-inf" if lo is None else f"{lo:g}"
        hi_txt = "+inf" if hi is None else f"{hi:g}"
        if not query.get("same_row_or_column", True):
            direct = "joint network bounds (not one straight line)"
        elif query.get("direct_complete"):
            direct = "direct slope data complete"
        else:
            direct = "direct segment shelved/missing slope data"
        msg = (f"A-B feasible dh in [{lo_txt}, {hi_txt}]  =>  "
               f"{query['classification'].upper()}  ({direct})")
        draw.text((30, height - 42), msg, font=bold, fill=(30, 26, 20, 255))

    # Scale bar: prefer archived ruler mm/pixel, else explicit station spacing.
    mm_per_px = options.get("ruler_mm_per_pixel")
    if ruler is not None and getattr(ruler, "per_pixel", None):
        try:
            mm_per_px = float(ruler.per_pixel)
        except (TypeError, ValueError):
            pass
    spacing_mm = options.get("station_spacing_mm")
    if mm_per_px and spacing_mm is None and result["columns"] > 1:
        try:
            bay_display_px = abs(positions[0][1][0] - positions[0][0][0])
            spacing_mm = (bay_display_px / factor) * float(mm_per_px)
        except (TypeError, ValueError):
            spacing_mm = None
    if spacing_mm:
        try:
            spacing_mm = float(spacing_mm)
            if mm_per_px:
                bar_px = spacing_mm * factor / float(mm_per_px)
            else:
                bar_px = 120
            bx0, by0 = width - bar_px - 40, 64
            draw.line((bx0, by0, bx0 + bar_px, by0), fill=(30, 26, 20, 255), width=6)
            draw.line((bx0, by0 - 8, bx0, by0 + 8), fill=(30, 26, 20, 255), width=4)
            draw.line((bx0 + bar_px, by0 - 8, bx0 + bar_px, by0 + 8), fill=(30, 26, 20, 255), width=4)
            draw.text((bx0 - 10, by0 + 12), f"station bay ~ {spacing_mm:g} mm", font=small,
                      fill=(30, 26, 20, 255))
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    return image.convert("RGB")


def _ascii(text: Any) -> bool:
    if not text:
        return False
    try:
        str(text).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def synthesize(settings: dict, base_image: Image.Image | None = None, ruler=None):
    result = solve_network(settings)
    options = parse_network(settings)[3] if isinstance(settings, dict) else {}
    picture = render_network(result, base_image=base_image, ruler=ruler, options=options)
    return result, picture
