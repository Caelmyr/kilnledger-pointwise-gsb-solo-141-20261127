"""Behavioural specification for the bidirectional height-map reconciler.

These tests encode the field requirements:

* horizontal and vertical readings are realised by one height map;
* walking every grid cell closes exactly;
* required segments are always kept in the default plan;
* the narrow-lip / wide-base conflict yields a default plus a mirror plan;
* two same-row/column stations receive tight bounds over *all* feasible maps;
* a step is confirmed, ruled out or inconclusive;
* shelved corridor edges are reported as missing direct slope data.

They are plain assert functions so they run under pytest or directly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiln.heightnet import (  # noqa: E402
    classify_step,
    query_pair,
    solve_network,
)


def lip_base_network():
    return {
        "rows": 2,
        "columns": 4,
        "tolerance": 0.01,
        "unit": "mm",
        "horizontal": [
            [{"value": 0.16, "label": "窄起唇", "required": True}, 0.06, 0.02],
            [{"value": 0.02, "label": "宽底实测甲"}, 0.01, 0.00],
        ],
        "vertical": [
            [{"value": 0.0, "required": True},
             {"value": -0.08, "required": True}, -0.13, -0.15],
        ],
        "query": {"start": [1, 0], "end": [1, 1], "step": 0.04},
    }


def test_default_keeps_required_lip_and_shelves_base_piece():
    result = solve_network(lip_base_network())
    assert "H:0:0" in result["kept"]          # narrow lip retained
    assert "H:1:0" in result["shelved"]       # one wide-base piece paused
    for required in result["required"]:
        assert required in result["kept"]
    assert result["honors_required"] is True


def test_heights_close_around_every_cell():
    result = solve_network(lip_base_network())
    assert result["closed"] is True
    for cell in result["closure"]["cells"]:
        assert abs(cell["height_residual"]) < 1e-6
    # Every retained reading is satisfied by the single height map.
    heights = result["heights"]
    for edge in result["edges"]:
        if edge["status"] != "kept":
            continue
        r, c = edge["row"], edge["col"]
        if edge["orientation"] == "H":
            delta = heights[r][c + 1] - heights[r][c]
        else:
            delta = heights[r + 1][c] - heights[r][c]
        assert edge["interval"][0] - 1e-6 <= delta <= edge["interval"][1] + 1e-6


def test_alternative_map_performs_mirror_swap():
    payload = lip_base_network()
    default = solve_network(payload)
    alternative = default["alternative"]
    assert alternative is not None
    assert "H:1:0" in alternative["kept"]
    assert "H:0:0" in alternative["shelved"]

    payload["variant"] = "alternative"
    selected = solve_network(payload)
    assert selected["variant"] == "alternative"
    assert "H:1:0" in selected["kept"]
    assert selected["closed"] is True


def test_shelved_corridor_is_flagged_missing_direct_slope():
    result = solve_network(lip_base_network())
    query = result["query"]
    assert query["direct_complete"] is False
    assert "H:1:0" in query["shelved_on_corridor"]
    assert query["missing_segments"]
    assert query["lower"] <= query["upper"]
    # Detour around the retained loop still bounds the pair.
    assert query["range"] == [0.05, 0.11]
    assert query["classification"] == "confirmed"


def test_alternative_restores_direct_corridor():
    payload = lip_base_network()
    payload["variant"] = "alternative"
    result = solve_network(payload)
    query = result["query"]
    assert query["direct_complete"] is True
    assert query["range"] == [0.01, 0.03]
    assert query["classification"] == "ruled_out"


def test_three_way_step_classification():
    assert classify_step(0.20, 0.40, 0.15)["classification"] == "confirmed"
    assert classify_step(-0.05, 0.08, 0.20)["classification"] == "ruled_out"
    assert classify_step(-0.30, 0.40, 0.20)["classification"] == "inconclusive"
    assert classify_step(0.05, 0.30, 0.20)["classification"] == "inconclusive"


def test_adjacent_kept_pair_range_is_reading_interval():
    payload = {
        "rows": 2, "columns": 2, "tolerance": 0.02,
        "horizontal": [[0.5], [0.1]],
        "vertical": [[0.0, 0.4]],
    }
    result = solve_network(payload)
    query = query_pair(
        2, 2, result["edge_catalog"], set(result["kept"]),
        {"start": [0, 0], "end": [0, 1]}, None, None, 0.02)
    # edge_catalog omits u/v; query_pair reconstructs node indices.
    assert query["range"] == [0.48, 0.52]
    assert query["direct_complete"] is True


def test_consistent_network_keeps_everything_without_alternative():
    payload = {
        "rows": 2, "columns": 3, "tolerance": 1e-4,
        "horizontal": [[0.1, 0.2], [-0.05, 0.05]],
        "vertical": [[0.0, -0.15, -0.3]],
    }
    result = solve_network(payload)
    assert result["shelved"] == []
    assert result["alternative"] is None
    assert result["closed"] is True
