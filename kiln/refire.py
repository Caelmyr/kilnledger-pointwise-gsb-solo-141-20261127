"""Refire planning against fired witness cells and stock-aware weighing checks.

The module is deliberately pure (dictionaries / Decimal / floats) so the same
rules serve the HTTP API, the recipe workbench and offline scripts.

Color model
-----------
The target photograph is only trustworthy up to a *global* Lab shift in the
lightness (L) and red-green (a) channels.  A plan picks one fired witness cell
per protected color and chooses one common ``(dL, da)`` correction inside the
supplied ranges so every spot stays inside its own ΔE76 limit.

Weighing model
--------------
Adopted recipes are never rewritten.  Already-weighed cups consume stock at
their recorded (Decimal-exact) amounts.  Remaining cups are re-checked against
scale graduations, a per-material half-division tolerance and stock caps.  When
no legal scale-mark combination fits, the sheet stops at ``pending`` (待核).
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
import itertools
import math

from .inputs import InputError, obj, seq


# ---------------------------------------------------------------------------
# Value parsing helpers
# ---------------------------------------------------------------------------

def dec(value, field="克数", *, low="0", high="1000000"):
    """Parse a finite non-negative decimal (strings keep their exact digits)."""
    if isinstance(value, bool):
        raise InputError(f"{field}需要是有限小数")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float, str)):
        try:
            result = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise InputError(f"{field}不是小数") from None
    else:
        raise InputError(f"{field}需要是有限小数")
    if not result.is_finite():
        raise InputError(f"{field}需要是有限小数")
    if result < Decimal(low) or result > Decimal(high):
        raise InputError(f"{field}需要介于 {low} 与 {high} 之间")
    return result


def ds(value):
    """Render a Decimal without scientific notation."""
    return format(value, "f")


def lab(value, field="Lab 色值"):
    parts = seq(value, field, minimum=3, maximum=3)
    try:
        values = [float(v) for v in parts]
    except (TypeError, ValueError):
        raise InputError(f"{field}需要三个数值") from None
    if not all(math.isfinite(v) for v in values):
        raise InputError(f"{field}需要是有限数值")
    low = (0.0, -128.0, -128.0)
    high = (100.0, 127.0, 127.0)
    for v, lo, hi in zip(values, low, high):
        if not lo <= v <= hi:
            raise InputError(f"{field}超出 Lab 合理范围")
    return tuple(values)


def _pick(data, *keys, default=None):
    for key in keys:
        if isinstance(data, dict) and key in data and data[key] is not None:
            return data[key]
    return default


def range_pair(value, field):
    pair = seq(value, field, minimum=2, maximum=2)
    lo, hi = float(pair[0]), float(pair[1])
    if not (math.isfinite(lo) and math.isfinite(hi)):
        raise InputError(f"{field}需要有限数值")
    if lo > hi:
        lo, hi = hi, lo
    return [lo, hi]


ROLE_ALIASES = {
    "base": "base", "background": "base", "ground": "base", "big": "base",
    "large": "base", "底色": "base", "大片底色": "base", "大面积底色": "base", "背景": "base",
    "band": "band", "stripe": "band", "strip": "band", "narrow": "band",
    "ribbon": "band", "line": "band", "色带": "band", "窄色带": "band", "彩带": "band",
    "spot": "spot", "point": "spot", "accent": "spot", "细节": "spot", "斑点": "spot",
}


def canonical_role(value):
    if value is None:
        return "spot"
    key = str(value).strip().casefold()
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    for alias, role in ROLE_ALIASES.items():
        if alias in str(value):
            return role
    return "spot"


# ---------------------------------------------------------------------------
# Normalized domain records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Target:
    key: str
    name: str
    lab: tuple
    limit: float
    role: str
    area: float

    def as_json(self):
        return {"id": self.key, "name": self.name, "lab": [round(v, 4) for v in self.lab],
                "limit": self.limit, "role": self.role, "area": self.area}


@dataclass(frozen=True)
class Candidate:
    key: str
    address: str
    label: str
    lab: tuple
    materials: tuple  # (name, Decimal adopted grams, Decimal exact target grams)

    def material_map(self):
        return {name: grams for name, grams, _ in self.materials}


def parse_ranges(raw):
    data = obj(raw, "复烧选方参数") if not isinstance(raw, dict) else raw
    block = _pick(data, "adjustment_ranges", "adjustmentRanges", "ranges", "adjustment", "correction", default=data)
    if not isinstance(block, dict):
        raise InputError("调整范围需要是对象")
    light = _pick(block, "L", "l", "lightness", "lightness_range", "dl", "dL", "明度", "明度范围")
    redgreen = _pick(block, "a", "A", "red_green", "redGreen", "red_green_range", "da", "红绿", "红绿范围")
    # Allow {"lightness":[lo,hi],"red_green":[lo,hi]} at the top level too.
    if light is None:
        light = _pick(data, "lightness_range", "lightness", "l_range", "明度范围")
    if redgreen is None:
        redgreen = _pick(data, "red_green_range", "redGreen_range", "red_green", "redGreen", "a_range", "红绿范围")
    if light is None or redgreen is None:
        raise InputError("请给出明度(L)与红绿(a)两项调整范围")
    return {"L": range_pair(light, "明度调整范围"), "a": range_pair(redgreen, "红绿调整范围")}


def parse_targets(raw, default_limit=None):
    if isinstance(raw, list):
        rows_raw = raw
        data = {}
    else:
        data = obj(raw, "复烧选方参数")
        rows_raw = _pick(data, "targets", "spots", "protected", "colors", "要保颜色")
    rows = seq(rows_raw, "要保的颜色", minimum=1, maximum=12)
    common_limit = _pick(data, "limit", "delta_e_limit", "max_delta_e", "tolerance", "color_limit")
    if common_limit is not None:
        default_limit = float(common_limit)
    targets = []
    used = set()
    for ix, item in enumerate(rows, 1):
        row = obj(item, "要保颜色")
        name = str(_pick(row, "name", "label", "title", default=f"颜色{ix}")).strip()[:40] or f"颜色{ix}"
        key = str(_pick(row, "id", "key", default=name))[:60]
        if key in used:
            key = f"{key}-{ix}"
        used.add(key)
        color = lab(_pick(row, "lab", "color_lab", "measured_lab"), f"「{name}」的 Lab")
        limit_value = _pick(row, "limit", "delta_e_limit", "max_delta_e", "tolerance", default=default_limit)
        if limit_value is None:
            raise InputError(f"「{name}」需要色差限额")
        limit = float(limit_value)
        if not math.isfinite(limit) or not 0.1 <= limit <= 100:
            raise InputError(f"「{name}」色差限额需要介于 0.1 与 100")
        area = float(_pick(row, "area", "weight", "size", default=1.0))
        if not math.isfinite(area) or area <= 0:
            area = 1.0
        role = canonical_role(_pick(row, "role", "kind", "type", "shape"))
        targets.append(Target(key, name, color, limit, role, area))
    return targets


def parse_materials(raw, *, field="用料"):
    """Return tuple of (name, adopted Decimal grams, exact-target Decimal grams)."""
    if isinstance(raw, dict):
        items = [{"name": name, **(value if isinstance(value, dict) else {"grams": value})}
                 for name, value in raw.items()]
    else:
        items = seq(raw, field, minimum=1, maximum=12)
    materials = []
    seen = set()
    for item in items:
        row = obj(item, "原料")
        name = str(_pick(row, "name", "material", "id")).strip()[:38]
        if not name:
            raise InputError("原料需要名称")
        fold = name.casefold()
        if fold in seen:
            raise InputError(f"原料 {name} 重复")
        seen.add(fold)
        adopted_raw = _pick(row, "grams", "amount", "mass", "actual_g", "weight")
        target_raw = _pick(row, "target_g", "target", "exact_g", "ideal_g")
        if adopted_raw is None and target_raw is None:
            # Parts-only recipes cannot occupy stock; reject clearly.
            if _pick(row, "parts", "ratio", "fraction") is not None:
                raise InputError(f"原料 {name} 请换算成每杯克数后再参与存量核对")
            raise InputError(f"原料 {name} 需要克数")
        target = dec(target_raw if target_raw is not None else adopted_raw, f"{name} 目标克数")
        adopted = dec(adopted_raw if adopted_raw is not None else ds(target), f"{name} 克数")
        materials.append((name, adopted, target))
    return tuple(materials)


def parse_candidates(raw):
    data = obj(raw, "复烧选方参数") if isinstance(raw, dict) else {}
    rows = seq(_pick(data, "candidates", "cells", "witnesses", "options", "旧实烧格"),
               "可对照的旧实烧格", minimum=1, maximum=72)
    candidates = []
    used = set()
    for ix, item in enumerate(rows, 1):
        row = obj(item, "旧实烧格")
        address = str(_pick(row, "address", "slot", "cell", "code", default=f"G{ix}")).strip()[:8]
        key = str(_pick(row, "id", "key", "candidate_id", default=_pick(row, "entry_id", default=address)))[:90]
        if key in used:
            key = f"{key}:{address}:{ix}"
        used.add(key)
        name = str(_pick(row, "label", "name", "title", default=address)).strip()[:60] or address
        color_raw = _pick(row, "lab", "fired_lab", "reading_lab", "color_lab")
        if color_raw is None and isinstance(row.get("reading"), dict):
            color_raw = row["reading"].get("lab")
        color = lab(color_raw, f"旧格 {address} 的实物 Lab")
        material_block = _pick(row, "materials", "weights", "material_weights", "amounts", "recipe", "formula")
        if isinstance(material_block, dict) and isinstance(material_block.get("materials"), (list, dict)):
            material_block = material_block["materials"]
        materials = parse_materials(material_block, field=f"旧格 {address} 用料")
        candidates.append(Candidate(key, address, name, color, materials))
    return candidates


def parse_stock(raw):
    if raw is None:
        return {}
    data = obj(raw, "原料存量")
    return {str(name).strip()[:38]: dec(value, f"原料 {name} 存量上限") for name, value in data.items()
            if str(name).strip()}


# ---------------------------------------------------------------------------
# Global Lab adjustment geometry
# ---------------------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _rect_distance2(px, py, bounds):
    llo, lhi = bounds["L"]
    alo, ahi = bounds["a"]
    dx = max(llo - px, 0.0, px - lhi)
    dy = max(alo - py, 0.0, py - ahi)
    return dx * dx + dy * dy


def pair_possible(target, candidate, bounds):
    """Cheap necessary test: some point of the range rectangle is within limit."""
    pl = candidate.lab[0] - target.lab[0]
    pa = candidate.lab[1] - target.lab[1]
    db = candidate.lab[2] - target.lab[2]
    radius2 = target.limit * target.limit - db * db
    if radius2 < -1e-8:
        return False
    radius2 = max(0.0, radius2)
    return _rect_distance2(pl, pa, bounds) <= radius2 + 1e-7


def _circle_line_x(cx, cy, r, x):
    inside = r * r - (x - cx) ** 2
    if inside < -1e-9:
        return []
    root = math.sqrt(max(0.0, inside))
    return [cy - root, cy + root]


def _circle_line_y(cx, cy, r, y):
    inside = r * r - (y - cy) ** 2
    if inside < -1e-9:
        return []
    root = math.sqrt(max(0.0, inside))
    return [cx - root, cx + root]


def _candidate_adjustment_points(centers, radii, bounds):
    llo, lhi = bounds["L"]
    alo, ahi = bounds["a"]
    points = [(llo, alo), (llo, ahi), (lhi, alo), (lhi, ahi)]
    # Projection of every disk center onto the allowed rectangle.
    for cx, cy in centers:
        points.append((_clamp(cx, llo, lhi), _clamp(cy, alo, ahi)))
    # Least-squares center (minimises sum of squared ΔE) and its projection.
    gx = sum(c[0] for c in centers) / len(centers)
    gy = sum(c[1] for c in centers) / len(centers)
    points.append((gx, gy))
    points.append((_clamp(gx, llo, lhi), _clamp(gy, alo, ahi)))
    # Circle / rectangle-edge intersections.
    for (cx, cy), r in zip(centers, radii):
        for x in (llo, lhi):
            for y in _circle_line_x(cx, cy, r, x):
                if alo - 1e-8 <= y <= ahi + 1e-8:
                    points.append((x, _clamp(y, alo, ahi)))
        for y in (alo, ahi):
            for x in _circle_line_y(cx, cy, r, y):
                if llo - 1e-8 <= x <= lhi + 1e-8:
                    points.append((_clamp(x, llo, lhi), y))
        # Point of the constraint circle facing the least-squares centre.
        dx, dy = gx - cx, gy - cy
        norm = math.hypot(dx, dy)
        if norm > 1e-12:
            points.append((cx + r * dx / norm, cy + r * dy / norm))
    # Circle / circle intersections.
    for i in range(len(centers)):
        x1, y1 = centers[i]
        r1 = radii[i]
        for j in range(i + 1, len(centers)):
            x2, y2 = centers[j]
            r2 = radii[j]
            dx, dy = x2 - x1, y2 - y1
            d = math.hypot(dx, dy)
            if d > r1 + r2 + 1e-8 or d < abs(r1 - r2) - 1e-8 or d < 1e-12:
                continue
            along = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
            h2 = r1 * r1 - along * along
            if h2 < -1e-8:
                continue
            h = math.sqrt(max(0.0, h2))
            mx, my = x1 + along * dx / d, y1 + along * dy / d
            px, py = -dy * h / d, dx * h / d
            points.append((mx + px, my + py))
            points.append((mx - px, my - py))
    # De-duplicate close points.
    unique = []
    for p in points:
        if not all(math.isfinite(v) for v in p):
            continue
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) > 1e-8 for q in unique):
            unique.append(p)
    return unique


def solve_adjustment(targets, chosen, bounds):
    """Return the best common (dL, da) for an assignment, or None if infeasible.

    ``chosen[i]`` is the Candidate matched to ``targets[i]``.  The optimum of
    the convex least-squares objective over the intersection of disks with the
    range rectangle is represented by the enumerated boundary/corner points.
    """
    centers, radii, db_offsets = [], [], []
    for target, cand in zip(targets, chosen):
        db = cand.lab[2] - target.lab[2]
        radius2 = target.limit * target.limit - db * db
        if radius2 < -1e-7:
            return None
        centers.append((cand.lab[0] - target.lab[0], cand.lab[1] - target.lab[1]))
        radii.append(math.sqrt(max(0.0, radius2)))
        db_offsets.append(db)

    best = None
    for dl, da in _candidate_adjustment_points(centers, radii, bounds):
        if not (bounds["L"][0] - 1e-8 <= dl <= bounds["L"][1] + 1e-8
                and bounds["a"][0] - 1e-8 <= da <= bounds["a"][1] + 1e-8):
            continue
        distances = [
            math.sqrt((dl - cx) ** 2 + (da - cy) ** 2 + db * db)
            for (cx, cy), db in zip(centers, db_offsets)
        ]
        if any(distance > target.limit + 1e-6
               for distance, target in zip(distances, targets)):
            continue
        objective = sum(d * d for d in distances)
        worst = max(distances)
        rank = (objective, worst, dl, da)
        if best is None or rank < best[0]:
            best = (rank, dl, da, distances)
    if best is None:
        return None
    _, dl, da, distances = best
    # Snap to four decimals while keeping every constraint valid.
    snapped = None
    for digits in (4, 5, 6, 9, 12):
        sdl, sda = round(dl, digits), round(da, digits)
        snapped_distances = [
            math.sqrt((target.lab[0] + sdl - cand.lab[0]) ** 2
                      + (target.lab[1] + sda - cand.lab[1]) ** 2
                      + (target.lab[2] - cand.lab[2]) ** 2)
            for target, cand in zip(targets, chosen)
        ]
        if all(d <= t.limit + 1e-9 for d, t in zip(snapped_distances, targets)) \
                and bounds["L"][0] - 1e-9 <= sdl <= bounds["L"][1] + 1e-9 \
                and bounds["a"][0] - 1e-9 <= sda <= bounds["a"][1] + 1e-9:
            snapped = (sdl, sda, snapped_distances)
            break
    if snapped is None:  # pragma: no cover - defensive
        snapped = (dl, da, distances)
    return snapped


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------

MAX_ASSIGNMENTS = 100_000


def _material_totals(assignment):
    totals = {}
    for cand in assignment:
        for name, grams, _ in cand.materials:
            totals[name] = totals.get(name, Decimal("0")) + grams
    return totals


def _plan_payload(index, targets, assignment, solved, stock, stock_ok):
    dl, da, distances = solved
    cells = []
    for target, cand, distance in zip(targets, assignment, distances):
        cells.append({
            "target_id": target.key,
            "target": target.name,
            "role": target.role,
            "address": cand.address,
            "candidate_id": cand.key,
            "label": cand.label,
            "target_lab": [round(v, 4) for v in target.lab],
            "fired_lab": [round(v, 4) for v in cand.lab],
            "delta_e": round(distance, 4),
            "limit": target.limit,
            "passes": bool(distance <= target.limit + 1e-9),
            "materials": [{"name": n, "grams": ds(g), "target_g": ds(t)} for n, g, t in cand.materials],
        })
    totals = _material_totals(assignment)
    material_rows = []
    for name in sorted(set(totals) | set(stock)):
        used = totals.get(name, Decimal("0"))
        cap = stock.get(name)
        remaining = (cap - used) if cap is not None else None
        row = {"name": name, "grams": ds(used), "within_stock": cap is None or used <= cap}
        if cap is not None:
            row["stock"] = ds(cap)
            row["remaining"] = ds(remaining)
        material_rows.append(row)
    signature = "|".join(f"{t.key}={c.address}" for t, c in zip(targets, assignment))
    cups = [{
        "cup_id": t.key, "label": t.name, "address": c.address, "role": t.role,
        "weights": [{"name": n, "grams": ds(g), "target_g": ds(tg)} for n, g, tg in c.materials],
    } for t, c in zip(targets, assignment)]
    return {
        "plan_id": f"plan-{index}",
        "signature": signature,
        "qualified": stock_ok,
        "stock_ok": stock_ok,
        "cells": cells,
        "cups": cups,
        "adjustment": {"L": round(dl, 4), "a": round(da, 4), "b": 0.0,
                       "dl": round(dl, 4), "da": round(da, 4)},
        "delta_e": [round(d, 4) for d in distances],
        "max_delta_e": round(max(distances), 4),
        "sum_delta_e": round(sum(distances), 4),
        "rms_delta_e": round(math.sqrt(sum(d * d for d in distances) / len(distances)), 4),
        "materials": material_rows,
        "stock_violations": [row for row in material_rows if not row["within_stock"]],
        "tags": [],
        "cost": {},
    }


def plan_refire(raw):
    """Compute every qualifying plan and the role-optimal alternatives.

    Input is a plain mapping; see module docstring / HTTP API for the schema.
    """
    data = obj(raw, "复烧选方参数")
    bounds = parse_ranges(data)
    raw_target_rows = seq(_pick(data, "targets", "spots", "protected", "colors", "要保颜色"),
                          "要保的颜色", minimum=1, maximum=12)
    targets = parse_targets(raw_target_rows,
                            default_limit=_pick(data, "limit", "delta_e_limit", "max_delta_e", "tolerance"))
    candidates = parse_candidates(data)
    stock = parse_stock(_pick(data, "stock", "inventory", "limits", "material_limits", "原料存量"))

    # Per-target candidate allow lists (explicit target.candidates / addresses).
    allowlists = []
    for target, raw_target in zip(targets, raw_target_rows):
        allowed = _pick(raw_target, "candidates", "options", "addresses", "allow")
        if allowed is None:
            allowlists.append(candidates)
            continue
        wanted = {str(v).strip() for v in seq(allowed, "可选旧格")}
        narrowed = [c for c in candidates if c.key in wanted or c.address in wanted or c.label in wanted]
        if not narrowed:
            raise InputError(f"「{target.name}」指定的旧实烧格不在候选中")
        allowlists.append(narrowed)

    viable_lists = []
    impossible = None
    for target, allowed in zip(targets, allowlists):
        viable = [c for c in allowed if pair_possible(target, c, bounds)]
        if not viable and impossible is None:
            impossible = target
        viable_lists.append(viable)
    if impossible is not None:
        return {"status": "no_color_match", "ok": False,
                "message": f"「{impossible.name}」在给定调整范围内没有任何旧格能进入色差限额",
                "adjustment_ranges": bounds,
                "targets": [t.as_json() for t in targets],
                "stock": {name: ds(value) for name, value in stock.items()},
                "plans": [], "alternatives": [], "stock_blocked": [],
                "counts": {"qualified": 0, "stock_blocked": 0, "assignments_checked": 0}}

    assignments = itertools.product(*viable_lists)
    qualified, stock_blocked, checked = [], [], 0
    for assignment in assignments:
        checked += 1
        if checked > MAX_ASSIGNMENTS:
            raise InputError("候选组合过多，请为每处颜色限定可对照的旧格")
        # Same candidate may serve several spots: that means brewing several
        # cups of it, so no de-duplication here.
        solved = solve_adjustment(targets, assignment, bounds)
        if solved is None:
            continue
        totals = _material_totals(assignment)
        stock_ok = all(used <= stock[name] for name, used in totals.items() if name in stock)
        payload = _plan_payload(checked, targets, assignment, solved, stock, stock_ok)
        if stock_ok:
            qualified.append(payload)
        else:
            stock_blocked.append(payload)

    if not qualified and not stock_blocked:
        return {"status": "no_color_match", "ok": False,
                "message": "没有找到同一份整图调整下让所有颜色都过限额的组合",
                "adjustment_ranges": bounds,
                "targets": [t.as_json() for t in targets],
                "plans": [], "alternatives": [], "stock_blocked": [],
                "assignments_checked": checked}

    qualified.sort(key=lambda p: (p["max_delta_e"], p["sum_delta_e"], p["signature"]))
    for ix, plan in enumerate(qualified, 1):
        plan["plan_id"] = f"plan-{ix}"

    # Role bests -----------------------------------------------------------
    def role_value(plan, role):
        values = [cell["delta_e"] for cell in plan["cells"] if cell["role"] == role]
        if not values:
            return None
        if role == "base":
            # Large area: area-weighted mean is closer to perceived fidelity.
            weighted = [(cell["delta_e"], next(t.area for t in targets if t.key == cell["target_id"]))
                        for cell in plan["cells"] if cell["role"] == "base"]
            return sum(d * a for d, a in weighted) / sum(a for _, a in weighted)
        return max(values)

    role_ids = {}
    for role in ("base", "band"):
        present = [p for p in qualified if role_value(p, role) is not None]
        if present:
            best = min(present, key=lambda p: (role_value(p, role), p["max_delta_e"], p["plan_id"]))
            role_ids[role] = best["plan_id"]
            if role == "base":
                best["tags"].append("base_best")
                best["tags"].append("保大片底色最佳")
            else:
                best["tags"].append("band_best")
                best["tags"].append("保住窄色带最佳")

    balanced_id = qualified[0]["plan_id"]
    qualified[0]["tags"].append("recommended")
    qualified[0]["tags"].append("推荐")
    role_ids["balanced"] = balanced_id

    best_base = min((role_value(p, "base") for p in qualified if role_value(p, "base") is not None), default=None)
    best_band = min((role_value(p, "band") for p in qualified if role_value(p, "band") is not None), default=None)
    best_max = min(p["max_delta_e"] for p in qualified)
    for plan in qualified:
        base_v = role_value(plan, "base")
        band_v = role_value(plan, "band")
        plan["cost"] = {
            "base_score": round(base_v, 4) if base_v is not None else None,
            "base_best_score": round(best_base, 4) if best_base is not None else None,
            "base_extra_delta_e": round(base_v - best_base, 4) if base_v is not None and best_base is not None else None,
            "band_score": round(band_v, 4) if band_v is not None else None,
            "band_best_score": round(best_band, 4) if best_band is not None else None,
            "band_extra_delta_e": round(band_v - best_band, 4) if band_v is not None and best_band is not None else None,
            "worst_extra_delta_e": round(plan["max_delta_e"] - best_max, 4),
        }

    stock_blocked.sort(key=lambda p: (p["max_delta_e"], p["sum_delta_e"]))
    for ix, plan in enumerate(stock_blocked, 1):
        plan["plan_id"] = f"stock-{ix}"
        plan["tags"] = ["stock_blocked", "超存量"]

    prefer = str(_pick(data, "prefer", "strategy", "choose", default="balanced")).strip().casefold()
    wanted_id = _pick(data, "plan_id", "selected_plan", "adopt")
    selected = None
    if wanted_id:
        selected = next((p for p in qualified if p["plan_id"] == wanted_id), None)
        if selected is None:
            raise InputError(f"没有编号为 {wanted_id} 的合格方案")
    elif prefer in ("base", "background", "底色", "base_best") and "base" in role_ids:
        selected = next(p for p in qualified if p["plan_id"] == role_ids["base"])
    elif prefer in ("band", "stripe", "色带", "band_best") and "band" in role_ids:
        selected = next(p for p in qualified if p["plan_id"] == role_ids["band"])
    else:
        selected = qualified[0]

    alternatives = [{
        "plan_id": p["plan_id"], "signature": p["signature"], "tags": p["tags"],
        "max_delta_e": p["max_delta_e"], "sum_delta_e": p["sum_delta_e"],
        "adjustment": p["adjustment"], "cost": p["cost"],
        "addresses": [cell["address"] for cell in p["cells"]],
    } for p in qualified]

    return {
        "status": "ready",
        "ok": True,
        "adjustment_ranges": bounds,
        "targets": [t.as_json() for t in targets],
        "stock": {name: ds(value) for name, value in stock.items()},
        "role_choices": role_ids,
        "selected_plan_id": selected["plan_id"],
        "plan": selected,
        "selected": selected,
        "chosen": selected,
        "adopted": selected,
        "plans": qualified,
        "alternatives": alternatives,
        "stock_blocked": stock_blocked[:12],
        "counts": {"qualified": len(qualified), "stock_blocked": len(stock_blocked),
                   "assignments_checked": checked},
    }


# ---------------------------------------------------------------------------
# Weighing re-verification
# ---------------------------------------------------------------------------

def _integer_units(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def legal_mark_units(target, precision, tolerance):
    """Scale-mark unit counts inside target ± tolerance (Decimal exact)."""
    center = target / precision
    half_units = tolerance / precision
    low = (center - half_units).to_integral_value(rounding=ROUND_CEILING)
    high = (center + half_units).to_integral_value(rounding=ROUND_FLOOR)
    start, stop = int(low), int(high)
    return [k for k in range(max(0, start), max(-1, stop) + 1) if k >= 0]


def _normalize_actuals(raw_actual):
    if raw_actual is None:
        return {}
    if isinstance(raw_actual, dict):
        return {str(n): dec(v, f"已称 {n}") for n, v in raw_actual.items()}
    result = {}
    for item in seq(raw_actual, "已称克数"):
        row = obj(item, "已称原料")
        name = str(_pick(row, "name", "material")).strip()
        value = _pick(row, "grams", "actual_g", "amount", "value")
        if name and value is not None:
            result[name] = dec(value, f"已称 {name}")
    return result


def _normalize_cups(data):
    """Return cups as ``[{"cup_id","label","address","rows":[...], "actual":{}}]``."""
    adopted_plan = _pick(data, "plan", "adopted", "selected_plan")
    raw_cups = _pick(data, "cups", "weigh_cups")
    if raw_cups is None and isinstance(adopted_plan, dict):
        raw_cups = adopted_plan.get("cups")
    if raw_cups is None and isinstance(adopted_plan, list):
        raw_cups = adopted_plan
    cups = []
    if raw_cups is not None:
        for ix, item in enumerate(seq(raw_cups, "杯", maximum=24), 1):
            row = obj(item, "杯")
            cup_id = str(_pick(row, "cup_id", "id", "key", default=f"cup-{ix}"))[:60]
            label = str(_pick(row, "label", "name", default=_pick(row, "address", default=f"第{ix}杯")))[:60]
            address = str(_pick(row, "address", "slot", default=""))[:8]
            weight_rows = parse_materials(_pick(row, "weights", "materials"), field=f"{label} 用料")
            actual = _normalize_actuals(_pick(row, "actual", "actuals", "weighed"))
            cups.append({"cup_id": cup_id, "label": label, "address": address,
                         "rows": weight_rows, "actual": actual})
    # Top-level weighed list/map can supply actuals by cup id / index.
    weighed_entries = _pick(data, "weighed", "weighed_cups", "actuals")
    if weighed_entries is not None and cups:
        if isinstance(weighed_entries, dict) and not (
                "weights" in weighed_entries or "cup_id" in weighed_entries
                or "id" in weighed_entries or "cup" in weighed_entries):
            # Mapping of {cup_id: {material: grams}}.
            entries = [{"cup_id": key, "weights": value} for key, value in weighed_entries.items()]
        elif isinstance(weighed_entries, list):
            entries = weighed_entries
        else:
            entries = [weighed_entries]
        for ix, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            ref = _pick(entry, "cup_id", "id", "cup", "index")
            target_cup = None
            if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
                pos = int(ref)
                target_cup = cups[pos] if pos < len(cups) else (cups[pos - 1] if 1 <= pos <= len(cups) else None)
            elif ref is not None:
                target_cup = next((c for c in cups if c["cup_id"] == str(ref) or c["label"] == str(ref)), None)
            if target_cup is None and ix < len(cups):
                target_cup = cups[ix]
            if target_cup is not None:
                extra = _normalize_actuals(_pick(entry, "weights", "actual", "actuals", default=entry))
                target_cup["actual"].update(extra)
    return cups


def recheck_weighing(raw):
    """Re-verify remaining cups; already-weighed actuals are preserved exactly."""
    data = obj(raw, "称量表参数")
    precision = dec(_pick(data, "precision", "scale_precision", "scale", "division",
                          "scale_division", "graduation", "刻度", default="0.1"),
                    "秤刻度", low="0.0001")
    explicit_tol = _pick(data, "tolerance_g", "tolerance_grams", "allowed_deviation", "tolerance")
    half_flag = _pick(data, "half_division", "half_cell", "half_graduation", "half", "半格", default=True)
    if explicit_tol is not None and not isinstance(explicit_tol, bool):
        tolerance = dec(explicit_tol, "每料可偏", low="0")
    elif isinstance(half_flag, bool):
        tolerance = precision / 2 if half_flag else precision
    elif half_flag is not None:
        # A numeric half-division value is read directly in grams.
        tolerance = dec(half_flag, "每料可偏半格", low="0")
    else:
        tolerance = precision / 2
    cup_grams_raw = _pick(data, "cup_grams", "grams", "target_grams", "每杯目标克数")
    cup_grams = dec(cup_grams_raw, "每杯目标克数") if cup_grams_raw is not None else None
    # The story's hard rule is per-material ±half-division; forcing the marks to
    # sum exactly to cup_grams is an opt-in extra.
    enforce_total = bool(_pick(data, "enforce_total", "exact_total", default=False))
    stock = parse_stock(_pick(data, "stock", "stock_limits", "material_stock", "upper_limits",
                              "inventory", "limits", "material_limits", "原料上限"))
    cups = _normalize_cups(data)
    if not cups:
        raise InputError("称量表至少需要一杯的用料")

    # Stock consumed by recorded actual amounts (Decimal exact, never altered).
    weighed_used = {name: Decimal("0") for name in stock}
    cup_payloads = []
    remaining_cups = []
    global_issues = []

    for cup in cups:
        actual = cup["actual"]
        is_weighed = bool(actual)
        for name, value in actual.items():
            weighed_used[name] = weighed_used.get(name, Decimal("0")) + value
        rows = []
        combos = []
        for name, adopted, target in cup["rows"]:
            units = legal_mark_units(target, precision, tolerance)
            options = [k * precision for k in units]
            actual_value = actual.get(name)
            actual_ok = None
            if actual_value is not None:
                actual_ok = abs(actual_value - target) <= tolerance
            row = {
                "name": name,
                "target_g": ds(target),
                "adopted_grams": ds(adopted),
                "tolerance_g": ds(tolerance),
                "range_g": [ds(max(Decimal("0"), target - tolerance)), ds(target + tolerance)],
                "options": [ds(o) for o in options],
                "actual_g": ds(actual_value) if actual_value is not None else None,
                "actual_within_tolerance": actual_ok,
                "on_scale": actual_value is None or (actual_value / precision) == (actual_value / precision).to_integral_value(),
            }
            rows.append(row)
            combos.append((name, units, adopted))
        payload = {
            "cup_id": cup["cup_id"], "label": cup["label"], "address": cup["address"],
            "state": "weighed" if is_weighed else "pending_check",
            "rows": rows,
            "actual": {n: ds(v) for n, v in actual.items()},
            "actual_total_g": ds(sum(actual.values(), Decimal("0"))) if actual else None,
            "issues": [], "verified": is_weighed,
        }
        cup_payloads.append(payload)
        if not is_weighed:
            remaining_cups.append((cup, payload, combos))
        else:
            payload["state"] = "weighed"
            for row in rows:
                if row["actual_within_tolerance"] is False:
                    payload["issues"].append({
                        "code": "actual_outside_tolerance", "material": row["name"],
                        "message": f"「{cup['label']}」的{row['name']}实称 {row['actual_g']}g 超出半格容差，已保留实数不改",
                    })

    # Actual amounts can never be silently fixed; over-cap stock is a review stop.
    for name, used in weighed_used.items():
        cap = stock.get(name)
        if cap is not None and used > cap:
            issue = {"code": "actual_over_stock", "material": name,
                     "message": f"已称原料 {name} 实数合计 {ds(used)}g 已超过上限 {ds(cap)}g"}
            global_issues.append(issue)

    available = {name: (stock[name] - weighed_used.get(name, Decimal("0"))) for name in stock}

    # Build legal mark combinations per remaining cup.
    cup_options = []
    for cup, payload, combos in remaining_cups:
        targets_by_name = {name: target for name, _, target in cup["rows"]}
        per_material_units = [units for _, units, _ in combos]
        if any(not units for units in per_material_units):
            for (name, units, adopted), row in zip(combos, payload["rows"]):
                if not units:
                    payload["issues"].append({"code": "no_legal_mark", "material": name,
                                             "message": f"{name} 在半格容差内没有可读的秤刻度"})
            payload["state"] = "pending"
            payload["verified"] = False
            cup_options.append(None)
            continue
        all_combos = list(itertools.product(*per_material_units))
        required_units = None
        if enforce_total and cup_grams is not None:
            quotient = cup_grams / precision
            if quotient != quotient.to_integral_value():
                payload["issues"].append({"code": "cup_grams_off_scale",
                                          "message": "每杯目标克数不是秤刻度的整数倍"})
            else:
                required_units = int(quotient)
                total_combos = [combo for combo in all_combos if sum(combo) == required_units]
                if not total_combos:
                    payload["issues"].append({"code": "total_not_within_half_division",
                                              "message": "各料在半格容差内无法同时凑齐每杯目标克数"})
                    cup_options.append(None)
                    payload["state"] = "pending"
                    payload["verified"] = False
                    continue
                all_combos = total_combos
        # Prefer the adopted scale marks, then least target deviation.
        adopted_units = tuple(
            int((adopted / precision).to_integral_value(rounding=ROUND_FLOOR))
            for _, _, adopted in combos
        )

        def combo_rank(combo):
            adopted_match = 0 if combo == adopted_units else 1
            deviation = sum(
                abs(k * precision - targets_by_name[name])
                for (name, _, _), k in zip(combos, combo)
            )
            return (adopted_match, deviation, combo)

        all_combos.sort(key=combo_rank)
        choices = []
        for combo in all_combos:
            material_amounts = {name: k * precision for (name, _, _), k in zip(combos, combo)}
            choices.append({"units": combo, "amounts": material_amounts})
        cup_options.append(choices if choices else None)

    # Smallest amount each remaining cup can legally take per material.
    minimum_remaining = {name: Decimal("0") for name in stock}
    for (cup, payload, combos), choices in zip(remaining_cups, cup_options):
        if choices:
            for name in {n for ch in choices for n in ch["amounts"]}:
                minimum_remaining[name] += min(ch["amounts"].get(name, Decimal("0")) for ch in choices)
        else:
            for name, units, _adopted in combos:
                if units:
                    minimum_remaining[name] += min(units) * precision

    # Joint stock feasibility across all remaining cups (small DFS).
    pending = bool(global_issues)
    ready_choices = []
    assignments = None
    if remaining_cups:
        if any(choices is None for choices in cup_options):
            pending = True
        else:
            def stock_fits(extra):
                for name, amount in extra.items():
                    cap = stock.get(name)
                    if cap is not None and amount > available.get(name, cap):
                        return False
                return True

            def dfs(ix, extra, picked):
                nonlocal assignments
                if assignments is not None:
                    return
                if ix == len(remaining_cups):
                    assignments = list(picked)
                    return
                for choice in cup_options[ix]:
                    merged = dict(extra)
                    for name, amount in choice["amounts"].items():
                        merged[name] = merged.get(name, Decimal("0")) + amount
                    if stock_fits(merged):
                        dfs(ix + 1, merged, picked + [choice])

            dfs(0, {}, [])
            if assignments is None:
                pending = True
                # Explain each material that cannot cover even its minimum marks.
                for name, cap in stock.items():
                    min_need = minimum_remaining.get(name, Decimal("0"))
                    if min_need > available.get(name, Decimal("0")):
                        global_issues.append({
                            "code": "insufficient_remaining_stock", "material": name,
                            "message": (f"{name} 已称后余料 {ds(available.get(name, Decimal('0')))}g，"
                                        f"剩余杯至少还需 {ds(min_need)}g，无法凑齐，停在待核"),
                        })
                for (cup, payload, combos), choices in zip(remaining_cups, cup_options):
                    payload["state"] = "pending"
                    payload["verified"] = False
                    if not payload["issues"]:
                        payload["issues"].append({"code": "stock",
                                                  "message": "原料余料无法凑齐这些杯，停在待核"})
            else:
                for ((cup, payload, combos), choice) in zip(remaining_cups, assignments):
                    for row, (name, units, adopted) in zip(payload["rows"], combos):
                        amount = choice["amounts"][name]
                        row["planned_grams"] = ds(amount)
                        row["adopted_kept"] = (amount == adopted)
                        row["stock_available_g"] = ds(available[name]) if name in available else None
                    payload["planned_total_g"] = ds(sum(choice["amounts"].values(), Decimal("0")))
                    if cup_grams is not None:
                        payload["total_target_g"] = ds(cup_grams)
                    payload["state"] = "ready"
                    payload["verified"] = True
                    ready_choices.append((payload, choice))

    # Aggregate stock rows.  With a feasible assignment show chosen amounts;
    # otherwise fall back to the legal minimum so shortfalls stay visible.
    feasible = assignments is not None or not remaining_cups
    planned_remaining = {name: Decimal("0") for name in stock}
    for payload, choice in ready_choices:
        for name, amount in choice["amounts"].items():
            planned_remaining[name] = planned_remaining.get(name, Decimal("0")) + amount
    stock_rows = []
    for name, cap in stock.items():
        used_actual = weighed_used.get(name, Decimal("0"))
        need_rest = planned_remaining.get(name, Decimal("0")) if feasible else minimum_remaining.get(name, Decimal("0"))
        after = cap - used_actual - need_rest
        sufficient = after >= 0 and (feasible or need_rest == 0 or after >= 0)
        if not sufficient:
            pending = True
        stock_rows.append({
            "name": name, "limit_g": ds(cap),
            "weighed_actual_g": ds(used_actual),
            "available_after_weighed_g": ds(cap - used_actual),
            "remaining_cups_g": ds(need_rest),
            "remaining_minimum_g": ds(minimum_remaining.get(name, Decimal("0"))),
            "remaining_is_minimum": not feasible,
            "remaining_after_g": ds(after),
            "sufficient": sufficient,
        })
        for payload in cup_payloads:
            for row in payload["rows"]:
                if row["name"] == name:
                    row["fits_stock"] = sufficient

    if global_issues:
        pending = True
    status = "pending" if pending else "ok"
    all_weighed = all(p["state"] == "weighed" for p in cup_payloads)
    if all_weighed and not global_issues:
        status = "complete"

    return {
        "status": status,
        "ok": not pending,
        "status_code": status,
        "needs_review": pending,
        "review": "待核" if pending else ("已称完" if status == "complete" else "已核对"),
        "status_label": {"ok": "已核对", "pending": "待核", "complete": "已称完"}[status],
        "recipe_locked": True,
        "precision": ds(precision),
        "tolerance_g": ds(tolerance),
        "half_division_g": ds(precision / 2),
        "cup_grams": ds(cup_grams) if cup_grams is not None else None,
        "enforce_total": enforce_total,
        "stock": stock_rows,
        "cups": cup_payloads,
        "issues": global_issues,
        "adopted_plan_id": _pick(data, "plan_id", "selected_plan_id",
                                 default=(_pick(data, "plan", default={}) or {}).get("plan_id")
                                 if isinstance(_pick(data, "plan", default={}), dict) else None),
    }


# ---------------------------------------------------------------------------
# Stored recipe entries + built-in demonstration scenario
# ---------------------------------------------------------------------------

def candidates_from_entry(entry):
    """Extract fired cells (Lab + per-cup grams) from an archived recipe entry."""
    payload = entry.get("payload", entry)
    result = payload.get("result", payload) if isinstance(payload, dict) else {}
    cells = result.get("cells") if isinstance(result, dict) else None
    entry_id = entry.get("id", "entry")
    out = []
    for cell in cells or []:
        reading = cell.get("reading") or {}
        color = reading.get("lab")
        weights = cell.get("weights") or []
        if not color or not weights:
            continue
        materials = []
        for w in weights:
            grams = w.get("grams")
            if grams is None:
                continue
            materials.append({"name": w["name"], "grams": grams,
                              "target_g": w.get("target_g", grams)})
        if not materials:
            continue
        out.append({
            "id": f"{entry_id}:{cell.get('address')}",
            "entry_id": entry_id,
            "address": cell.get("address"),
            "label": cell.get("address"),
            "lab": color,
            "materials": materials,
        })
    return out


def demo_scenario():
    """A small deterministic base-vs-band trade-off used by the workbench demo.

    The cross choice (A1+B3) is the most accurate but stock-blocked; A1+B4 is
    best for the large base and A2+B3 best for the narrow band.  Iron stock is
    tuned exactly to the base plan so an upper half-division actual on cup one
    stops the second cup at 待核.
    """
    return {
        "adjustment_ranges": {"L": [-3.0, 3.0], "a": [-2.0, 2.0]},
        "targets": [
            {"id": "base", "name": "大片底色", "role": "base", "area": 12.0, "limit": 2.4,
             "lab": [62.0, 8.0, 22.0]},
            {"id": "band", "name": "窄色带", "role": "band", "area": 1.0, "limit": 2.8,
             "lab": [44.0, 18.0, 28.0]},
        ],
        "candidates": [
            {"id": "old-A1", "address": "A1", "label": "A1 暖底",
             "lab": [63.12, 7.42, 22.12],
             "materials": [{"name": "长石", "grams": "5.6", "target_g": "5.58"},
                           {"name": "石英", "grams": "2.4", "target_g": "2.42"},
                           # 1.05 sits between marks 1.0 / 1.1 ...
                           {"name": "木灰", "grams": "1.1", "target_g": "1.05"},
                           # ... and 0.95 between 0.9 / 1.0; adopted pair totals 2.0.
                           {"name": "氧化铁", "grams": "0.9", "target_g": "0.95"}]},
            {"id": "old-A2", "address": "A2", "label": "A2 浅暖底",
             "lab": [63.9, 8.6, 22.7],
             "materials": [{"name": "长石", "grams": "5.2"}, {"name": "石英", "grams": "2.2"},
                           {"name": "木灰", "grams": "1.9", "target_g": "1.86"},
                           {"name": "氧化铁", "grams": "0.6", "target_g": "0.62"}]},
            {"id": "old-B3", "address": "B3", "label": "B3 琥珀带",
             "lab": [45.08, 17.42, 28.12],
             "materials": [{"name": "长石", "grams": "4.6"}, {"name": "石英", "grams": "2.0"},
                           {"name": "木灰", "grams": "2.5"},
                           {"name": "氧化铁", "grams": "0.9", "target_g": "0.88"}]},
            {"id": "old-B4", "address": "B4", "label": "B4 深赭带",
             "lab": [45.9, 16.9, 28.7],
             "materials": [{"name": "长石", "grams": "4.4"}, {"name": "石英", "grams": "1.9"},
                           {"name": "木灰", "grams": "3.1"},
                           # 0.58 only admits the 0.6 mark within half a division.
                           {"name": "氧化铁", "grams": "0.6", "target_g": "0.58"}]},
        ],
        # A1(0.9)+B4(0.6) == 1.5 and A2(0.6)+B3(0.9) == 1.5; A1+B3 = 1.8 blocked.
        "stock": {"长石": "20", "石英": "10", "木灰": "10", "氧化铁": "1.5"},
        "precision": "0.1",
        "cup_grams": "10",
        "demo_weighed": [
            {"cup_id": "base", "weights": {"长石": "5.6", "石英": "2.4", "木灰": "1.0", "氧化铁": "1.0"}},
        ],    }


# --- Descriptive aliases for alternate callers / tests ---------------------
build_refire_plans = plan_refire
refire_plan = plan_refire
choose_refire = plan_refire
generate_plans = plan_refire
recheck_weights = recheck_weighing
verify_weights = recheck_weighing
verify_weighing = recheck_weighing
weigh_table = recheck_weighing
reweigh = recheck_weighing
check_cups = recheck_weighing
