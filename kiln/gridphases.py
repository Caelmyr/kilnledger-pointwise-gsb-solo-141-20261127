"""Exhaustive grid adjudication for glaze phases.

The grid has at most 8 x 8 cells. Solid cells carry the set of reference
phases whose colour difference limit accepts the cell reading; every other
cell stays empty. The solver enumerates the phase assignments that satisfy
the user's confirmed same-phase groups (each group must be one phase and
four-edge connected) and the warm horizontal / cold vertical band policy.

The search is a plain backtracking search with forward checking. It is kept
in pure Python with no third-party dependencies so the combinatorial
guarantees can be unit tested without image data.
"""

from dataclasses import dataclass, field

from .inputs import InputError, choice, integer, obj, seq


MAX_GRID_SIDE = 8
DEFAULT_NODE_BUDGET = 400_000
DEFAULT_SOLUTION_LIMIT = 256
BAND_POLICIES = ("both", "horizontal", "vertical", "none")


@dataclass(frozen=True)
class Group:
    name: str
    cells: tuple
    phase: str | None  # None means the group may resolve to any common phase

    @property
    def open(self):
        return self.phase is None


@dataclass(frozen=True)
class Band:
    name: str
    phase: str
    orientation: str  # "horizontal" or "vertical"
    span: int
    center: bool
    cells: tuple
    required: bool = True


@dataclass(frozen=True)
class GridProblem:
    rows: int
    columns: int
    phases: tuple
    domains: dict
    groups: tuple = ()
    bands: tuple = ()

    def neighbors(self, cell):
        row, column = cell
        if row > 0:
            yield row - 1, column
        if row + 1 < self.rows:
            yield row + 1, column
        if column > 0:
            yield row, column - 1
        if column + 1 < self.columns:
            yield row, column + 1

    def connected(self, cells, assignment):
        members = set(cells)
        start = next(iter(members))
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbor in self.neighbors(current):
                if neighbor in members and neighbor not in seen and assignment.get(neighbor) == assignment[start]:
                    seen.add(neighbor)
                    stack.append(neighbor)
        return seen == members

    def group_ok(self, group, assignment):
        if not all(cell in assignment for cell in group.cells):
            return True  # checked again once the assignment is complete
        values = {assignment[cell] for cell in group.cells}
        if len(values) != 1:
            return False
        if group.phase is not None and next(iter(values)) != group.phase:
            return False
        return self.connected(group.cells, assignment)

    def band_ok(self, band, assignment):
        active = [cell for cell in band.cells if cell in assignment]
        if len(active) != len(band.cells):
            return True
        values = {assignment[cell] for cell in active}
        if len(values) != 1 or next(iter(values)) != band.phase:
            return False
        return self.connected(active, assignment)

    def valid_complete(self, assignment, active_bands):
        for group in self.groups:
            if not self.group_ok(group, assignment):
                return False
        for band in self.bands:
            if band in active_bands and not self.band_ok(band, assignment):
                return False
        return True


def _cell(raw, rows, columns, field_name):
    data = seq(raw, field_name, minimum=2, maximum=2)
    row = integer(data[0], field_name + "行", low=0, high=rows - 1)
    column = integer(data[1], field_name + "列", low=0, high=columns - 1)
    return row, column


def read_groups(raw, rows, columns, known_phases):
    groups = []
    names = set()
    for index, item in enumerate(seq(raw, "同相连片", maximum=24)):
        data = obj(item, "同相连片")
        name = str(data.get("name") or f"同相组{index + 1}")
        if name in names:
            raise InputError(f"同相连片名称重复：{name}")
        names.add(name)
        cells = tuple(
            _cell(part, rows, columns, f"「{name}」格")
            for part in seq(data.get("cells"), f"「{name}」格", minimum=2, maximum=MAX_GRID_SIDE * MAX_GRID_SIDE)
        )
        if len(set(cells)) != len(cells):
            raise InputError(f"「{name}」内有重复格")
        phase = data.get("phase")
        if phase is not None:
            phase = choice(phase, f"「{name}」指定釉相", known_phases)
        groups.append(Group(name, cells, phase))
    return tuple(groups)


def read_bands(raw, rows, columns, known_phases):
    bands = []
    used = set()
    for index, item in enumerate(seq(raw, "试样带", maximum=4)):
        data = obj(item, "试样带")
        name = str(data.get("name") or f"试样带{index + 1}")
        if name in used:
            raise InputError(f"试样带名称重复：{name}")
        used.add(name)
        phase = choice(data.get("phase"), f"「{name}」釉相", known_phases)
        orientation = choice(data.get("orientation", "horizontal"), f"「{name}」走向",
                             ("horizontal", "vertical"))
        span = integer(data.get("span", 1), f"「{name}」带宽", low=1,
                       high=rows if orientation == "horizontal" else columns)
        if (span % 2) == 0:
            raise InputError(f"「{name}」带宽需要是奇数，正中才唯一")
        middle = integer(data.get("at", (rows - 1) // 2 if orientation == "horizontal" else (columns - 1) // 2),
                         f"「{name}」中轴", low=0,
                         high=rows - 1 if orientation == "horizontal" else columns - 1)
        half = span // 2
        low, high = middle - half, middle + half
        if low < 0 or high >= (rows if orientation == "horizontal" else columns):
            raise InputError(f"「{name}」超出网格边界")
        if orientation == "horizontal":
            cells = tuple((line, column) for line in range(low, high + 1) for column in range(columns))
        else:
            cells = tuple((row, line) for line in range(low, high + 1) for row in range(rows))
        bands.append(Band(name, phase, orientation, span, middle == ((rows - 1) // 2 if orientation == "horizontal"
                          else (columns - 1) // 2), cells))
    return tuple(bands)


def band_conflicts(problem):
    """Pairs of bands that cannot both hold because a solid cell crosses them."""
    conflicts = []
    for i, first in enumerate(problem.bands):
        if first.phase == "":
            continue
        for second in problem.bands[i + 1:]:
            if first.phase == second.phase or first.orientation == second.orientation:
                continue
            crossing = set(first.cells) & set(second.cells)
            blocked = [cell for cell in crossing if cell in problem.domains]
            if blocked and first.phase != second.phase:
                conflicts.append((first, second, tuple(sorted(blocked))))
    return conflicts


@dataclass
class SearchResult:
    feasible: bool
    assignments: list
    solution_count: int
    exhausted: bool
    nodes: int
    active_bands: tuple = ()

    @property
    def proven(self):
        return self.exhausted

    @property
    def example(self):
        return self.assignments[0] if self.assignments else None


def _restrict_domains(problem, active_bands):
    domains = {cell: set(values) for cell, values in problem.domains.items()}
    for group in problem.groups:
        if group.phase is not None:
            for cell in group.cells:
                if cell not in domains:
                    raise InputError(f"「{group.name}」含留空格，无法固定为同一釉相")
                domains[cell] = {group.phase} & domains[cell]
                if not domains[cell]:
                    raise InputError(f"「{group.name}」的指定釉相不在这些格的可接受色样内")
    for band in active_bands:
        for cell in band.cells:
            if cell not in domains:
                continue  # empty cells are skipped in the band check
            domains[cell] = {band.phase} & domains[cell]
            if not domains[cell]:
                return None, band
    for group in problem.groups:
        if group.open:
            shared = set(intersection_all(domains[cell] for cell in group.cells
                                          if cell in domains))
            if not shared:
                return None, group
    return domains, None


def intersection_all(iterable):
    iterator = iter(iterable)
    try:
        result = set(next(iterator))
    except StopIteration:
        return set()
    for values in iterator:
        result &= set(values)
    return result


def _connected_possible(problem, group, domains):
    """Open groups must still share at least one phase over edge-adjacent cells."""
    if group.phase is not None:
        shared = {group.phase}
    else:
        shared = intersection_all(domains[cell] for cell in group.cells if cell in domains)
    members = set(group.cells)
    for phase in shared:
        start = next(iter(members))
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbor in problem.neighbors(current):
                if neighbor in members and neighbor not in seen and phase in domains.get(neighbor, ()):
                    seen.add(neighbor)
                    stack.append(neighbor)
        if seen == members:
            return True
    return False


def _pick_variable(variables, index, domains):
    """Minimum-remaining-values cell among the still-unassigned suffix."""
    position = min(range(index, len(variables)), key=lambda k: len(domains[variables[k]]))
    return position


def solve(problem, *, active_bands=(), node_budget=DEFAULT_NODE_BUDGET,
          solution_limit=DEFAULT_SOLUTION_LIMIT):
    domains, _blocker = _restrict_domains(problem, active_bands)
    if domains is None:
        return SearchResult(False, [], 0, True, 0, tuple(active_bands))
    for group in problem.groups:
        if not _connected_possible(problem, group, domains):
            return SearchResult(False, [], 0, True, 0, tuple(active_bands))

    variables = sorted(domains)
    assignments_found = []
    counters = {"nodes": 0, "exhausted": True}
    assignment = {}

    def partial_group_ok():
        for group in problem.groups:
            if group.phase is None:
                continue
            fixed = [cell for cell in group.cells if cell in assignment]
            if fixed and any(assignment[cell] != group.phase for cell in fixed):
                return False
        for band in active_bands:
            fixed = [cell for cell in band.cells if cell in assignment]
            if fixed and any(assignment[cell] != band.phase for cell in fixed):
                return False
        return True

    def backtrack(index, working):
        if counters["nodes"] >= node_budget or len(assignments_found) >= solution_limit:
            counters["exhausted"] = False
            return
        if index == len(variables):
            if problem.valid_complete(assignment, active_bands):
                assignments_found.append(dict(assignment))
            else:
                counters["nodes"] += 1
            return
        position = _pick_variable(variables, index, working)
        cell = variables[position]
        variables[index], variables[position] = variables[position], variables[index]
        counters["nodes"] += 1
        for phase in sorted(working[cell]):
            assignment[cell] = phase
            if partial_group_ok():
                backtrack(index + 1, working)
            del assignment[cell]
            if not counters["exhausted"]:
                variables[index], variables[position] = variables[position], variables[index]
                return
        variables[index], variables[position] = variables[position], variables[index]

    backtrack(0, domains)
    return SearchResult(
        bool(assignments_found), assignments_found,
        len(assignments_found) if counters["exhausted"] else len(assignments_found),
        counters["exhausted"], counters["nodes"], tuple(active_bands),
    )


def _policy_bands(problem, policy):
    if policy == "both":
        return tuple(problem.bands), ()
    if policy == "none":
        return (), tuple(problem.bands)
    kept = tuple(band for band in problem.bands
                 if (band.orientation == "horizontal") == (policy == "horizontal"))
    relaxed = tuple(band for band in problem.bands if band not in kept)
    return kept, relaxed


def _detour(problem, relaxed_bands, blockers):
    hints = []
    for band in relaxed_bands:
        if band.orientation == "vertical":
            blocked_columns = sorted({cell[1] for pair in blockers for cell in pair[2]})
            options = [column for column in range(problem.columns) if column not in blocked_columns]
            hints.append({
                "band": band.name, "orientation": band.orientation,
                "blocked_columns": blocked_columns,
                "suggested_columns": options,
                "note": "竖带改走未被横带占据的列，并保持带宽与边相连",
            })
        else:
            blocked_rows = sorted({cell[0] for pair in blockers for cell in pair[2]})
            options = [row for row in range(problem.rows) if row not in blocked_rows]
            hints.append({
                "band": band.name, "orientation": band.orientation,
                "blocked_rows": blocked_rows,
                "suggested_rows": options,
                "note": "横带改走未被竖带占据的行，并保持带宽与边相连",
            })
    return hints


def adjudicate(problem, policy="horizontal", *, node_budget=DEFAULT_NODE_BUDGET,
               solution_limit=DEFAULT_SOLUTION_LIMIT):
    """Run the policy and, on conflict, the alternative family.

    Returns a dict with the chosen default family and every feasible family
    so the caller can switch to another qualified diagram.
    """
    policy = choice(policy, "试样带策略", BAND_POLICIES)
    conflicts = band_conflicts(problem)
    families = {}
    order = [policy] + [candidate for candidate in BAND_POLICIES if candidate != policy]
    default_name = None
    for name in order:
        active, relaxed = _policy_bands(problem, name)
        result = solve(problem, active_bands=active, node_budget=node_budget,
                       solution_limit=solution_limit)
        families[name] = {
            "policy": name,
            "feasible": result.feasible,
            "proven": result.proven,
            "solution_count": result.solution_count,
            "search_nodes": result.nodes,
            "active_bands": [band.name for band in active],
            "relaxed_bands": [band.name for band in relaxed],
            "example": result.example,
            "assignments": result.assignments,
            "detour": _detour(problem, relaxed, conflicts) if not result.feasible or relaxed else [],
        }
        if default_name is None and result.feasible:
            default_name = name
    requested = families[policy]
    return {
        "policy": policy,
        "default_family": default_name,
        "requested_feasible": requested["feasible"],
        "conflicts": [
            {"first": first.name, "second": second.name,
             "orientation": [first.orientation, second.orientation],
             "blocking_cells": [list(cell) for cell in blocked]}
            for first, second, blocked in conflicts
        ],
        "families": families,
    }


def _extreme_phase_area(problem, weight, target_phase, *, maximize, active_bands, node_budget):
    domains, blocker = _restrict_domains(problem, active_bands)
    if domains is None:
        return {"feasible": False, "proven": True, "blocker": getattr(blocker, "name", str(blocker))}
    for group in problem.groups:
        if not _connected_possible(problem, group, domains):
            return {"feasible": False, "proven": True}

    variables = sorted(domains)
    counters = {"nodes": 0}
    bound = float("-inf") if maximize else float("inf")
    best_assignment = None
    proven = True
    assignment = {}
    weighted = {cell: float(weight.get(cell, 0.0)) for cell in variables}

    def score():
        return sum(weighted[cell] for cell, phase in assignment.items() if phase == target_phase)

    def walk(index):
        nonlocal bound, best_assignment, proven
        counters["nodes"] += 1
        if counters["nodes"] > node_budget:
            proven = False
            return
        if index == len(variables):
            if not problem.valid_complete(assignment, active_bands):
                return
            value = score()
            better = value > bound if maximize else value < bound
            if best_assignment is None or better:
                bound = value
                best_assignment = dict(assignment)
            return
        cell = variables[_pick_variable(variables, index, domains)]
        position = variables.index(cell)
        variables[index], variables[position] = variables[position], variables[index]
        current = score()
        remaining = sum(weighted[item] for item in variables[index:]
                        if target_phase in domains[item] and item != cell)
        # Branch ordering: try the target phase first when maximising.
        ordered = sorted(domains[cell], key=lambda phase: (phase != target_phase, phase))
        if maximize:
            ordered.reverse()
        for phase in ordered:
            assignment[cell] = phase
            new_score = current + (weighted[cell] if phase == target_phase else 0.0)
            if maximize:
                optimistic = new_score + remaining
                if best_assignment is not None and optimistic <= bound:
                    del assignment[cell]
                    continue
            else:
                if best_assignment is not None and new_score >= bound:
                    del assignment[cell]
                    continue
            walk(index + 1)
            del assignment[cell]
            if not proven:
                variables[index], variables[position] = variables[position], variables[index]
                return
        variables[index], variables[position] = variables[position], variables[index]

    walk(0)
    if best_assignment is None:
        return {"feasible": False, "proven": proven, "search_nodes": counters["nodes"]}
    return {
        "feasible": True, "proven": proven,
        "value": bound, "assignment": best_assignment,
        "search_nodes": counters["nodes"],
    }


def minimum_phase_area(problem, weight, target_phase, *, active_bands=(),
                       node_budget=DEFAULT_NODE_BUDGET):
    """Exact minimum target-phase overlap area over every feasible diagram."""
    return _extreme_phase_area(problem, weight, target_phase, maximize=False,
                               active_bands=active_bands, node_budget=node_budget)


def maximum_phase_area(problem, weight, target_phase, *, active_bands=(),
                       node_budget=DEFAULT_NODE_BUDGET):
    """Exact maximum target-phase overlap area over every feasible diagram."""
    return _extreme_phase_area(problem, weight, target_phase, maximize=True,
                               active_bands=active_bands, node_budget=node_budget)
