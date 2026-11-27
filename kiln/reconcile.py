"""Reconcile several gridded photographs of one physical tray.

Each photograph already carries a single-tray transcription (see :mod:`kiln.trays`).
The photos may be rotated by a right angle relative to the master tray and may
only show part of it.  This module enumerates every whole-cell placement of the
photos on one master grid that simultaneously satisfies:

* per-cell observations (transcribed coupon, confirmed empty cell, covered cell);
* manual clues (same master cell, different master cells, shared-cell count);
* the requirement that one physical coupon never occupies two master cells.

Covered positions stay unknown; an empty cell can never overlap a cell that
shows a coupon.
"""

from dataclasses import dataclass

from .inputs import Conflict, InputError, choice, distinct, integer, key, obj, optional_text, seq, text
from .trays import Tray, cell_code


MAX_VIEWS = 6
MAX_ROWS = 6
MAX_SOLUTIONS = 1500
ROTATIONS = (0, 90, 180, 270)
CLUE_KINDS = ("same", "different", "shared")
VISIBLE_STATES = ("identified", "unread")


def rotate_local(row, column, rows, columns, rotation):
    """Return the master-grid coordinates of a local photo cell after rotation."""
    if rotation == 0:
        return row, column
    if rotation == 90:
        return column, rows - 1 - row
    if rotation == 180:
        return rows - 1 - row, columns - 1 - column
    return columns - 1 - column, row


def rotated_extent(rows, columns, rotation):
    """Master-grid (bottom, exclusive right) occupied by a rotated view."""
    if rotation in (0, 180):
        return rows, columns
    return columns, rows


@dataclass(frozen=True)
class ViewCell:
    view: int
    row: int
    column: int

    @property
    def label(self):
        return f"v{self.view + 1}:{cell_code(self.row, self.column)}"


@dataclass(frozen=True)
class Placement:
    view: int
    rotation: int
    top: int
    left: int
    cells: tuple  # tuple of (ViewCell, master row, master column)


class Reconcile:
    """Parsed reconciliation request independent of persistence."""

    def __init__(self, user, tray_records, photos):
        user = obj(user, "拼合抄录参数")
        self.name = text(user.get("name", "托盘拼合抄录"), "拼合名称", limit=65)
        self.rows = integer(user.get("rows"), "总盘行数", low=1, high=MAX_ROWS)
        self.columns = integer(user.get("columns"), "总盘列数", low=1, high=MAX_ROWS)
        self.note = optional_text(user.get("note"), "拼合备注", 400)
        self.views = self._read_views(user, tray_records, photos)
        self.clues = self._read_clues(user)
        self._validate_clues()
        self.solutions = []
        self.truncated = False
        self._result_cache = None

    # ------------------------------------------------------------------ input
    def _read_views(self, user, tray_records, photos):
        raw_views = seq(user.get("views"), "拼合照片", minimum=3, maximum=MAX_VIEWS)
        distinct([key(item.get("tray"), "托盘来源") for item in raw_views], "拼合照片")
        views = []
        for index, item in enumerate(raw_views):
            entry = tray_records[key(item.get("tray"), "托盘来源")]
            photo = photos[entry["photo_id"]]
            tray = Tray.read(entry["payload"], photo["width"], photo["height"])
            if tray.rows > self.rows or tray.columns > self.columns:
                raise InputError(f"照片 {tray.name} 的网格比总盘还大")
            rotations = tuple(dict.fromkeys(
                integer(value, "直角转向", low=0, high=270)
                for value in seq(item.get("rotations", ROTATIONS), "直角转向", minimum=1, maximum=4)
            ))
            if any(value not in ROTATIONS for value in rotations):
                raise InputError("直角转向只可取 0 / 90 / 180 / 270")
            views.append({"index": index, "entry": entry, "photo": photo, "tray": tray,
                          "rotations": rotations})
        return views

    def _read_clues(self, user):
        clues = []
        for raw in seq(user.get("clues", []), "拼合线索", maximum=40):
            item = obj(raw, "拼合线索")
            kind = choice(item.get("kind"), "线索类型", CLUE_KINDS)
            if kind == "shared":
                first = self._ref(item.get("a"), "线索照片格")
                second = self._ref(item.get("b"), "线索照片格")
                count = integer(item.get("count"), "共见格数", low=1, high=MAX_ROWS * MAX_ROWS)
                clues.append({"kind": kind, "a": first, "b": second, "count": count})
            else:
                refs = tuple(self._ref(value, "线索照片格")
                             for value in seq(item.get("cells"), "线索照片格", minimum=2, maximum=8))
                distinct([ref.label for ref in refs], "线索照片格")
                clues.append({"kind": kind, "cells": refs})
        return clues

    def _ref(self, value, field):
        raw = obj(value, field)
        view = integer(raw.get("view"), "照片序号", low=0, high=len(self.views) - 1)
        tray = self.views[view]["tray"]
        row = integer(raw.get("row"), "行", high=tray.rows - 1)
        column = integer(raw.get("column"), "列", high=tray.columns - 1)
        return ViewCell(view, row, column)

    def observation(self, ref):
        return self.views[ref.view]["tray"].cell(ref.row, ref.column)

    def _validate_clues(self):
        for clue in self.clues:
            refs = (clue["cells"] if "cells" in clue else (clue["a"], clue["b"]))
            # Two different cells of one photo can never land on one master cell.
            if clue["kind"] == "same":
                per_view = {}
                for ref in refs:
                    per_view.setdefault(ref.view, set()).add((ref.row, ref.column))
                if any(len(cells) > 1 for cells in per_view.values()):
                    raise InputError("同一张照片里的不同格不可能是同格，请检查同格线索")
                if any(self.observation(ref).state == "empty" for ref in refs) and \
                        any(self.observation(ref).state in VISIBLE_STATES for ref in refs):
                    raise Conflict("同格线索把空格和看见试片的格放在了一起")
            if clue["kind"] == "shared":
                limit = min(self.views[clue["a"].view]["tray"].rows
                            * self.views[clue["a"].view]["tray"].columns,
                            self.views[clue["b"].view]["tray"].rows
                            * self.views[clue["b"].view]["tray"].columns)
                if clue["count"] > limit:
                    raise InputError("共见格数超过较小照片的格数")

    # ------------------------------------------------------------- placements
    def candidates(self, view_index):
        """All distinct (rotation, offset) placements for one photograph."""
        view = self.views[view_index]
        tray = view["tray"]
        found = {}
        for rotation in view["rotations"]:
            bottom, right = rotated_extent(tray.rows, tray.columns, rotation)
            for top in range(0, self.rows - bottom + 1):
                for left in range(0, self.columns - right + 1):
                    cells = []
                    for cell in tray.cells:
                        master_row, master_column = rotate_local(
                            cell.row, cell.column, tray.rows, tray.columns, rotation)
                        cells.append((ViewCell(view_index, cell.row, cell.column),
                                      top + master_row, left + master_column))
                    found[(rotation, top, left)] = Placement(view_index, rotation, top, left, tuple(cells))
        return tuple(found.values())

    # ------------------------------------------------------------- constraints
    def _check_clue(self, clue, mapping):
        if clue["kind"] == "same":
            targets = [mapping.get(ref) for ref in clue["cells"]]
            if any(target is None for target in targets):
                return True  # a referenced view is not placed yet; re-check later
            return len(set(targets)) == 1
        if clue["kind"] == "different":
            targets = [mapping.get(ref) for ref in clue["cells"]]
            if any(target is None for target in targets):
                return True
            return len(set(targets)) == len(targets)
        a, b = mapping.get(clue["a"]), mapping.get(clue["b"])
        if a is None or b is None:
            return True
        cells_a = {position for ref, position in mapping.items() if ref.view == clue["a"].view}
        cells_b = {position for ref, position in mapping.items() if ref.view == clue["b"].view}
        return len(cells_a & cells_b) == clue["count"]

    def _placement_conflicts(self, placement, master, coupon_cells, mapping):
        local = {}
        for ref, row, column in placement.cells:
            cell = self.observation(ref)
            position = (row, column)
            local[ref] = position
            current = master.get(position)
            if current is not None:
                other_states = current["states"]
                if cell.state == "empty" and other_states & set(VISIBLE_STATES):
                    return True
                if cell.state in VISIBLE_STATES and "empty" in other_states:
                    return True
                if cell.state == "identified" and "identified" in other_states:
                    if cell.coupon.casefold() not in current["coupons"]:
                        return True
            if cell.state == "identified":
                coupon = cell.coupon.casefold()
                prior = coupon_cells.get(coupon)
                if prior and prior != {position}:
                    return True
        trial = {**mapping, **local}
        for clue in self.clues:
            touched = clue["cells"] if "cells" in clue else (clue["a"], clue["b"])
            if any(ref.view == placement.view for ref in touched) and not self._check_clue(clue, trial):
                return True
        return False

    def _apply(self, placement, master, coupon_cells):
        new_master = {position: {"states": set(entry["states"]), "coupons": set(entry["coupons"]),
                                 "refs": list(entry["refs"])}
                      for position, entry in master.items()}
        new_coupons = {coupon: set(positions) for coupon, positions in coupon_cells.items()}
        for ref, row, column in placement.cells:
            cell = self.observation(ref)
            position = (row, column)
            entry = new_master.setdefault(position, {"states": set(), "coupons": set(), "refs": []})
            entry["states"].add(cell.state)
            entry["refs"].append(ref)
            if cell.state == "identified":
                entry["coupons"].add(cell.coupon.casefold())
                new_coupons.setdefault(cell.coupon.casefold(), set()).add(position)
        return new_master, new_coupons

    def solve(self):
        options = [self.candidates(index) for index in range(len(self.views))]
        if any(not group for group in options):
            raise Conflict("有照片在允许的转向下放不进总盘网格")
        order = sorted(range(len(options)), key=lambda idx: len(options[idx]))
        state = {"master": {}, "coupons": {}}
        mapping = {}
        placements = [None] * len(self.views)

        def backtrack(depth):
            if self.truncated:
                return
            if depth == len(order):
                for clue in self.clues:
                    if not self._check_clue(clue, mapping):
                        return
                self.solutions.append(tuple(placements[idx] for idx in range(len(self.views))))
                if len(self.solutions) >= MAX_SOLUTIONS:
                    self.truncated = True
                return
            view_index = order[depth]
            for placement in options[view_index]:
                if self._placement_conflicts(placement, state["master"], state["coupons"], mapping):
                    continue
                saved_master, saved_coupons = state["master"], state["coupons"]
                state["master"], state["coupons"] = self._apply(
                    placement, state["master"], state["coupons"])
                for ref, row, column in placement.cells:
                    mapping[ref] = (row, column)
                placements[view_index] = placement
                backtrack(depth + 1)
                placements[view_index] = None
                for ref, _, _ in placement.cells:
                    mapping.pop(ref, None)
                state["master"], state["coupons"] = saved_master, saved_coupons
                if self.truncated:
                    return

        backtrack(0)
        if not self.solutions:
            raise Conflict("没有任何摆法能同时满足全部照片与线索，请放宽转向或线索")
        return self.result()

    # ---------------------------------------------------------------- output
    def result(self):
        if self._result_cache is not None:
            return self._result_cache
        rotations_used = {index: sorted({solution[index].rotation for solution in self.solutions})
                         for index in range(len(self.views))}
        self._result_cache = {
            "name": self.name, "rows": self.rows, "columns": self.columns, "note": self.note,
            "solution_count": len(self.solutions), "truncated": self.truncated,
            "max_solutions": MAX_SOLUTIONS,
            "view_rotations": [{"view": index, "rotations": rotations_used[index]}
                               for index in range(len(self.views))],
            "views": [self._view_summary(index) for index in range(len(self.views))],
            "clues": self._clue_json(),
            "cells": self._possibilities(),
            "layouts": [self._layout_json(solution) for solution in self.solutions],
        }
        return self._result_cache

    def _view_summary(self, index):
        view = self.views[index]
        tray = view["tray"]
        return {
            "view": index, "tray_id": view["entry"]["id"], "tray_revision": view["entry"]["revision"],
            "photo_id": view["photo"]["id"], "photo_title": view["photo"]["title"],
            "tray_name": tray.name, "rows": tray.rows, "columns": tray.columns,
            "allowed_rotations": list(view["rotations"]),
        }

    def _clue_json(self):
        output = []
        for clue in self.clues:
            if clue["kind"] == "shared":
                output.append({"kind": "shared", "a": clue["a"].label, "b": clue["b"].label,
                               "count": clue["count"]})
            else:
                output.append({"kind": clue["kind"], "cells": [ref.label for ref in clue["cells"]]})
        return output

    def _possibilities(self):
        """Aggregate what each master cell can be across every surviving layout."""
        total = len(self.solutions)
        aggregate = {}
        for row in range(self.rows):
            for column in range(self.columns):
                aggregate[(row, column)] = {
                    "states": set(), "coupons": set(), "refs": set(),
                    "empty_in": 0, "visible_in": 0, "covered_only_in": 0, "unseen_in": 0,
                    "coupon_layouts": {},
                }
        for layout_index, solution in enumerate(self.solutions):
            occupied = {}
            for placement in solution:
                for ref, row, column in placement.cells:
                    occupied.setdefault((row, column), []).append(ref)
            for row in range(self.rows):
                for column in range(self.columns):
                    entry = aggregate[(row, column)]
                    refs = occupied.get((row, column))
                    if not refs:
                        entry["unseen_in"] += 1
                        continue
                    cells = [self.observation(ref) for ref in refs]
                    states = {cell.state for cell in cells}
                    entry["states"].update(states)
                    entry["refs"].update(refs)
                    for cell in cells:
                        if cell.state == "identified":
                            entry["coupons"].add(cell.coupon)
                            entry["coupon_layouts"].setdefault(cell.coupon, set()).add(layout_index)
                    if states == {"covered"}:
                        entry["covered_only_in"] += 1
                    if "empty" in states:
                        entry["empty_in"] += 1
                    if states & set(VISIBLE_STATES):
                        entry["visible_in"] += 1
        result = []
        for row in range(self.rows):
            for column in range(self.columns):
                entry = aggregate[(row, column)]
                fixed = sorted(coupon for coupon, layouts in entry["coupon_layouts"].items()
                               if len(layouts) == total)
                result.append({
                    "row": row, "column": column, "address": cell_code(row, column),
                    "possible_states": sorted(entry["states"]),
                    "possible_coupons": sorted(entry["coupons"]),
                    "fixed_coupon": fixed[0] if len(fixed) == 1 else "",
                    "empty_in_all": 0 < entry["empty_in"] == total,
                    "covered_in_all": entry["covered_only_in"] == total,
                    "never_seen": entry["unseen_in"] == total,
                    "view_cells": sorted(ref.label for ref in entry["refs"]),
                })
        return result

    def _layout_json(self, solution):
        placements = []
        for placement in solution:
            placements.append({
                "view": placement.view, "rotation": placement.rotation,
                "top": placement.top, "left": placement.left,
                "mapping": [{"view_cell": ref.label, "row": row, "column": column,
                             "address": cell_code(row, column)}
                            for ref, row, column in placement.cells],
            })
        return {"placements": placements}

    def payload(self):
        return {
            "name": self.name, "rows": self.rows, "columns": self.columns, "note": self.note,
            "views": [{"tray": view["entry"]["id"], "rotations": list(view["rotations"])}
                      for view in self.views],
            "clues": self._clue_payload(),
        }

    def _clue_payload(self):
        output = []
        for clue in self.clues:
            if clue["kind"] == "shared":
                output.append({"kind": "shared",
                               "a": {"view": clue["a"].view, "row": clue["a"].row, "column": clue["a"].column},
                               "b": {"view": clue["b"].view, "row": clue["b"].row, "column": clue["b"].column},
                               "count": clue["count"]})
            else:
                output.append({"kind": clue["kind"], "cells": [
                    {"view": ref.view, "row": ref.row, "column": ref.column} for ref in clue["cells"]]})
        return output


def load_request(store, firing, raw):
    """Build a Reconcile from an API payload, resolving stored tray entries."""
    envelope = obj(raw, "拼合抄录请求")
    user = obj(envelope["拼合抄录"], "拼合抄录") if "拼合抄录" in envelope else envelope
    raw_views = seq(user.get("views"), "拼合照片", minimum=3, maximum=MAX_VIEWS)
    records, photos = {}, {}
    for item in raw_views:
        entry = store.entry(key(item.get("tray"), "托盘来源"))
        if entry["kind"] != "tray" or entry["firing_id"] != firing["id"]:
            raise InputError("拼合照片必须使用当前窑次的托盘抄录")
        photo = store.photo(entry["photo_id"])
        records[entry["id"]] = entry
        photos[photo["id"]] = photo
    return Reconcile(user, records, photos)
