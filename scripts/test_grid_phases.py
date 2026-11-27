"""Exhaustive checks for the grid phase solver and overlap geometry.

Run with: python scripts/test_grid_phases.py
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiln.gridareas import GridGeometry, overlap_area, polygon_area
from kiln.gridphases import (
    GridProblem, Group, Band, adjudicate, maximum_phase_area, minimum_phase_area,
)
from kiln.geometry import Box


FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append((name, detail))
        print(f"FAIL {name} {detail}")


def problem_with_bands():
    # 5x5 grid. Warm horizontal band row 2, cold vertical band column 2.
    # Crossing cell (2, 2) accepts both phases, so the two bands cannot both hold.
    rows = columns = 5
    phases = ("warm", "cold")
    domains = {(r, c): {"warm", "cold"} for r in range(rows) for c in range(columns)}
    horizontal = Band("warm-row", "warm", "horizontal", 1, True,
                      tuple((2, c) for c in range(columns)))
    vertical = Band("cold-col", "cold", "vertical", 1, True,
                    tuple((r, 2) for r in range(rows)))
    problem = GridProblem(rows, columns, phases, domains, (), (horizontal, vertical))
    # Every non-crossing cell is forced toward its band phase only where needed;
    # keep full ambiguity elsewhere and rely on the crossing contradiction.
    return problem


def test_band_conflict():
    problem = problem_with_bands()
    outcome = adjudicate(problem, "horizontal")
    check("default family feasible", outcome["default_family"] in ("horizontal", "vertical", "none"))
    check("both family infeasible at crossing", not outcome["families"]["both"]["feasible"])
    check("horizontal family feasible", outcome["families"]["horizontal"]["feasible"])
    check("vertical family feasible", outcome["families"]["vertical"]["feasible"])
    check("conflict reported at centre", outcome["conflicts"][0]["blocking_cells"] == [[2, 2]])
    horizontal = outcome["families"]["horizontal"]
    check("horizontal example keeps warm row",
          all(horizontal["example"][(2, c)] == "warm" for c in range(5)))
    check("detour lists centre column as blocked",
          horizontal["detour"][0]["blocked_columns"] == [2])


def test_group_connectivity():
    # Group cells are only diagonally adjacent, so no connected assignment exists.
    domains = {(0, 0): {"a", "b"}, (1, 1): {"a", "b"}, (2, 2): {"a"}}
    problem = GridProblem(3, 3, ("a", "b"), domains,
                          (Group("diagonal", ((0, 0), (1, 1), (2, 2)), None),), ())
    outcome = adjudicate(problem, "none")
    check("diagonal group rejected", outcome["default_family"] is None)

    edge_cells = ((0, 0), (0, 1), (1, 1))
    domains = {cell: {"a", "b"} for cell in edge_cells}
    problem = GridProblem(3, 3, ("a", "b"), domains,
                          (Group("edge", edge_cells, None),), ())
    outcome = adjudicate(problem, "none")
    check("edge group feasible", outcome["families"]["none"]["feasible"])
    assignment = outcome["families"]["none"]["example"]
    check("edge group single phase", len({assignment[cell] for cell in edge_cells}) == 1)


def test_fixed_phase_group():
    from kiln.inputs import InputError
    domains = {(0, 0): {"a"}, (0, 1): {"a", "b"}}
    problem = GridProblem(2, 2, ("a", "b"), domains,
                          (Group("fixed", ((0, 0), (0, 1)), "b"),), ())
    try:
        adjudicate(problem, "none")
    except InputError:
        print("PASS fixed phase outside domain rejected")
        return
    print("FAIL fixed phase outside domain rejected")
    FAILURES.append(("fixed phase outside domain rejected", "no InputError"))


def test_area_extremes():
    # 2x2 grid, phase x possible everywhere; gate covers the full grid (area 4).
    domains = {(r, c): {"x", "y"} for r in range(2) for c in range(2)}
    problem = GridProblem(2, 2, ("x", "y"), domains)
    weights = {cell: 1.0 for cell in domains}
    low = minimum_phase_area(problem, weights, "x")
    high = maximum_phase_area(problem, weights, "x")
    check("minimum x area is zero", low["feasible"] and low["value"] == 0.0)
    check("maximum x area is four", high["feasible"] and high["value"] == 4.0)
    check("extremes proven exhaustively", low["proven"] and high["proven"])


def test_gate_with_group_guarantee():
    # A fixed x group forces two cells; minimum guaranteed x area must be 2.
    domains = {(0, 0): {"x", "y"}, (0, 1): {"x", "y"}, (1, 0): {"x", "y"}, (1, 1): {"x", "y"}}
    group = Group("kept", ((0, 0), (0, 1)), "x")
    problem = GridProblem(2, 2, ("x", "y"), domains, (group,), ())
    weights = {cell: 1.0 for cell in domains}
    low = minimum_phase_area(problem, weights, "x")
    check("group guarantees two cells", low["value"] == 2.0)


def test_geometry_overlap():
    geometry = GridGeometry(4, 4, Box(0, 0, 40, 40))
    cell = geometry.cell_polygon(0, 0)
    check("full cell area", abs(overlap_area(cell, Box(0, 0, 40, 40).vertices()) - 100.0) < 1e-9)
    half = Box(0, 0, 5, 10).vertices()
    check("quarter overlap", abs(overlap_area(cell, half) - 50.0) < 1e-9)
    triangle = ((0, 0), (10, 0), (0, 10))
    check("triangle overlap", abs(overlap_area(cell, triangle) - 50.0) < 1e-9)


def test_diagonal_touch_does_not_connect():
    domains = {(0, 0): {"a"}, (1, 1): {"a"}}
    problem = GridProblem(2, 2, ("a",), domains,
                          (Group("touch", ((0, 0), (1, 1)), "a"),), ())
    outcome = adjudicate(problem, "none")
    check("corner-only touch rejected", outcome["default_family"] is None)


def test_release_needs_every_diagram():
    # Two ambiguous cells in the gate; a single diagram can fill both with x,
    # but the guaranteed minimum over all feasible diagrams is zero.
    domains = {(0, 0): {"x", "y"}, (0, 1): {"x", "y"}}
    problem = GridProblem(1, 2, ("x", "y"), domains)
    weights = {cell: 1.0 for cell in domains}
    low = minimum_phase_area(problem, weights, "x")
    high = maximum_phase_area(problem, weights, "x")
    check("working diagram can reach full area", high["value"] == 2.0)
    check("guaranteed minimum stays zero", low["value"] == 0.0)
    check("0.5 gate cannot be released", not (low["proven"] and low["value"] / 2.0 >= 0.5))


def main():
    tests = (
        test_band_conflict, test_group_connectivity, test_fixed_phase_group,
        test_area_extremes, test_gate_with_group_guarantee, test_geometry_overlap,
        test_diagonal_touch_does_not_connect, test_release_needs_every_diagram,
    )
    for test in tests:
        try:
            test()
        except Exception:  # pragma: no cover - reporting path
            FAILURES.append((test.__name__, traceback.format_exc()))
            print(f"ERROR {test.__name__}")
    if FAILURES:
        print(f"\n{len(FAILURES)} failing checks")
        for name, detail in FAILURES:
            print(name, detail)
        sys.exit(1)
    print("\nall grid phase checks passed")


if __name__ == "__main__":
    main()
