"""End-to-end check of grid phase adjudication on a synthetic surface."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image

from kiln.calibration import Correction
from kiln.gridphase import inspect


def make_surface():
    # 8x8 cell grid over 160x160 px. Warm band on row 3, cold band on column 3.
    image = Image.new("RGBA", (160, 160), (0, 0, 0, 255))
    pixels = np.zeros((160, 160, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    warm = np.array([214, 128, 70], dtype=np.uint8)
    cold = np.array([72, 128, 168], dtype=np.uint8)
    neutral = np.array([170, 150, 130], dtype=np.uint8)
    pixels[..., :3] = neutral
    for row in range(8):
        for column in range(8):
            y0, x0 = row * 20, column * 20
            if row == 3:
                pixels[y0:y0 + 20, x0:x0 + 20, :3] = warm
            if column == 3:
                pixels[y0:y0 + 20, x0:x0 + 20, :3] = cold
    # Crossing cell keeps an ambiguous midtone so both bands conflict there.
    pixels[60:80, 60:80, :3] = (warm.astype(int) + cold.astype(int)) // 2
    return Image.fromarray(pixels, "RGBA")


def main():
    image = make_surface()
    warm_ref = [60, 60, 100, 80]
    cold_ref = [60, 80, 100, 100]
    settings = {
        "region": [0, 0, 160, 160],
        "rows": 8, "columns": 8,
        "solid_coverage": 0.6,
        "band_policy": "horizontal",
        "phases": [
            {"name": "warm", "limit": 26, "region": warm_ref, "marker": "#e89477"},
            {"name": "cold", "limit": 26, "region": cold_ref, "marker": "#67b8aa"},
        ],
        "groups": [
            {"name": "warm-stretch", "phase": "warm",
             "cells": [[3, 0], [3, 1], [3, 2]]},
        ],
        "bands": [
            {"name": "warm-row", "phase": "warm", "orientation": "horizontal", "span": 1, "at": 3},
            {"name": "cold-col", "phase": "cold", "orientation": "vertical", "span": 1, "at": 3},
        ],
        "area_gates": [
            {"name": "warm-row-share", "phase": "warm", "minimum_fraction": 0.10,
             "frame": [0, 60, 160, 80]},
        ],
        "audit_frame": [0, 0, 80, 80],
    }
    result, diagram = inspect(image, settings, Correction.identity())
    assert result["default_family"] == "horizontal", result["default_family"]
    assert not result["families"][[i for i, f in enumerate(result["families"])
                                   if f["policy"] == "both"][0]]["feasible"]
    solid = [cell for cell in result["cells"] if cell["solid"]]
    print("solid cells:", len(solid), "blank:", sum(cell["blank"] for cell in result["cells"]))
    assert len(solid) >= 50
    gate = result["release"]["gates"][0]
    print("gate:", gate)
    assert gate["proven"] and gate["passed"], gate
    assert result["release"]["status"] == "releasable", result["release"]["status"]
    audit = result["audit_frame"]
    assert audit is not None and len(audit["phases"]) == 2
    for line in audit["phases"]:
        print("audit", line)
    horizontal = next(f for f in result["families"] if f["policy"] == "horizontal")
    assert horizontal["detour"], "vertical band detour advice expected"
    print("detour:", horizontal["detour"])
    diagram.save("/tmp/grid-phase-diagram.png")
    print("diagram size:", diagram.size)
    print("end-to-end grid phase check passed")


if __name__ == "__main__":
    main()
