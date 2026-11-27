"""Compatibility entry point for the closed height-map synthesiser.

The implementation lives in :mod:`kiln.heightnet`; this module exposes the
same operations under the "height map" vocabulary used in the workbench.
"""

from .heightnet import (  # noqa: F401
    build_height_map,
    build_plan,
    classify_step,
    closure_report,
    find_swap,
    floyd_bounds,
    greedy_select,
    is_feasible,
    is_network_payload,
    parse_network,
    query_pair,
    reconcile,
    render_network,
    solve_network,
    synthesize,
)

# Primary verb aliases.
solve_height_map = solve_network
height_map = solve_network
analyze_height_map = synthesize
height_range = query_pair
pair_range = query_pair

__all__ = [
    "solve_network", "solve_height_map", "height_map", "reconcile", "build_height_map",
    "synthesize", "analyze_height_map", "query_pair", "height_range", "pair_range",
    "classify_step", "closure_report", "floyd_bounds", "is_feasible", "greedy_select",
    "find_swap", "parse_network", "render_network", "is_network_payload", "build_plan",
]
