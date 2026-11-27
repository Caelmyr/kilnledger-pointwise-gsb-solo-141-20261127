"""Grid-based glaze phase adjudication on a photographed surface.

This complements the pixel-level ``phases`` tool. The same reference colours
and per-phase delta-E limits are reused, but the surface is read through an
at-most 8 x 8 grid. Solid cells normalise to one phase chosen from every
reference that accepts the cell colour; the other cells stay empty. The
combinatorial solver keeps confirmed same-phase groups edge-connected and
handles the warm horizontal / cold vertical band conflict. Archive area
requirements are certified against every feasible diagram rather than the
single displayed working diagram.
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .calibration import cielab, delta_e, html_color, linearize, weighted_means
from .formulations import Phase
from .geometry import Region
from .gridareas import (
    AreaGate, GridGeometry, frame_polygon, gate_weights, polygon_area, specimen_requirements,
)
from .gridphases import (
    BAND_POLICIES, GridProblem, adjudicate, maximum_phase_area, minimum_phase_area,
    read_bands, read_groups,
)
from .imaging import window
from .inputs import InputError, choice, distinct, integer, number, obj, seq


MAX_CELL_PIXELS = 240_000
DIAGRAM_SIDE = 720


def _phase_refs(raw, image, correction):
    entries = seq(raw, "釉相参照", minimum=2, maximum=7)
    phases = [Phase.read(item, image, correction, index) for index, item in enumerate(entries)]
    distinct([phase.name.casefold() for phase in phases], "釉相名")
    return phases


def cell_readings(image, region, rows, columns, correction, solid_coverage):
    sample = window(image, region.as_json(), max_pixels=1_500_000).require_pixels(24)
    geometry = GridGeometry(rows, columns, region.bounds)
    cell_width = max(1, round(geometry.cell_width))
    cell_height = max(1, round(geometry.cell_height))
    scale = min(1.0, math.sqrt(MAX_CELL_PIXELS / max(1, cell_width * cell_height)))
    cell_width = max(2, round(cell_width * scale))
    cell_height = max(2, round(cell_height * scale))
    corrected = correction.apply(linearize(sample.rgb))
    lab_image = cielab(corrected)
    cells = {}
    for row in range(rows):
        for column in range(columns):
            box = geometry.cell_box(row, column)
            left = max(0, round(box.left - sample.left))
            top = max(0, round(box.top - sample.top))
            right = min(sample.width, round(box.right - sample.left))
            bottom = min(sample.height, round(box.bottom - sample.top))
            if right <= left or bottom <= top:
                cells[(row, column)] = {"solid": False, "coverage_fraction": 0.0, "lab": None,
                                        "delta_e": {}}
                continue
            patch = sample.weights[top:bottom, left:right]
            coverage = float(patch.sum(dtype=np.float64))
            fraction = coverage / max(1e-9, patch.size)
            if fraction < solid_coverage:
                cells[(row, column)] = {"solid": False, "coverage_fraction": round(fraction, 5),
                                        "lab": None, "delta_e": {}}
                continue
            lab_patch = lab_image[top:bottom, left:right]
            center = weighted_means(lab_patch, patch)
            cells[(row, column)] = {
                "solid": True,
                "coverage_fraction": round(fraction, 5),
                "lab": [round(float(value), 4) for value in center],
                "pixel_area": round(coverage, 3),
                "center": center,
            }
    return geometry, cells, sample


def build_domains(cells, phases):
    domains = {}
    for address, reading in cells.items():
        if not reading["solid"]:
            continue
        distances = {
            phase.name: float(delta_e(reading["center"], phase.lab.astype(np.float32)))
            for phase in phases
        }
        reading["delta_e"] = {name: round(value, 4) for name, value in distances.items()}
        accepted = tuple(name for name, value in distances.items() if value <= next(
            phase.limit for phase in phases if phase.name == name))
        reading["candidates"] = list(accepted)
        if accepted:
            domains[address] = set(accepted)
    return domains


def _serialise_assignment(assignment):
    if assignment is None:
        return None
    return {f"{row},{column}": phase for (row, column), phase in sorted(assignment.items())}


def _certify_gate(gate, problem, family, geometry, solid_cells, node_budget):
    active = tuple(band for band in problem.bands if band.name in family["active_bands"])
    weights = gate_weights(geometry, gate, solid_cells)
    low = minimum_phase_area(problem, weights, gate.phase, active_bands=active, node_budget=node_budget)
    high = maximum_phase_area(problem, weights, gate.phase, active_bands=active, node_budget=node_budget)
    if not low.get("feasible") or not high.get("feasible"):
        # A truncated search that never reached a complete diagram is not proof
        # of infeasibility: report it as inconclusive rather than a failure.
        proven = bool(low.get("proven") and high.get("proven"))
        if not proven:
            return {"name": gate.name, "phase": gate.phase, "status": "inconclusive",
                    "minimum_fraction": None, "maximum_fraction": None, "proven": False,
                    "required_fraction": gate.minimum_fraction,
                    "certifying_family": family["policy"]}
        return {"name": gate.name, "phase": gate.phase, "status": "infeasible_family",
                "minimum_fraction": None, "maximum_fraction": None, "proven": True,
                "required_fraction": gate.minimum_fraction,
                "certifying_family": family["policy"]}
    minimum_fraction = low["value"] / gate.region_area
    maximum_fraction = high["value"] / gate.region_area
    proven = bool(low["proven"] and high["proven"])
    return {
        "name": gate.name, "phase": gate.phase,
        "minimum_fraction": round(minimum_fraction, 6),
        "maximum_fraction": round(maximum_fraction, 6),
        "required_fraction": gate.minimum_fraction,
        "proven": proven,
        "passed": proven and minimum_fraction + 1e-12 >= gate.minimum_fraction,
        "certifying_family": family["policy"],
        "extreme_nodes": [low["search_nodes"], high["search_nodes"]],
    }


def _frame_report(polygon, problem, family, geometry, solid_cells, phases, node_budget):
    active = tuple(band for band in problem.bands if band.name in family["active_bands"])
    weights = {cell: geometry.overlap(cell[0], cell[1], polygon) for cell in solid_cells}
    report = []
    for phase in phases:
        low = minimum_phase_area(problem, weights, phase.name, active_bands=active, node_budget=node_budget)
        high = maximum_phase_area(problem, weights, phase.name, active_bands=active, node_budget=node_budget)
        if not low.get("feasible"):
            continue
        report.append({
            "phase": phase.name,
            "minimum_fraction": round(low["value"] / polygon_area(polygon), 6),
            "maximum_fraction": round(high["value"] / polygon_area(polygon), 6),
            "proven": bool(low["proven"] and high["proven"]),
        })
    return report


def inspect(image, settings, correction):
    data = obj(settings, "格网釉相判读")
    phases = _phase_refs(data.get("phases"), image, correction)
    rows = integer(data.get("rows", 6), "行数", low=2, high=8)
    columns = integer(data.get("columns", 6), "列数", low=2, high=8)
    solid_coverage = number(data.get("solid_coverage", 0.62), "实底格覆盖阈值", low=0.05, high=1.0)
    node_budget = integer(data.get("node_budget", 400_000), "搜索节点上限", low=1000, high=5_000_000)
    region = Region.read(data.get("region"), *image.size)
    geometry, cells, sample = cell_readings(image, region, rows, columns, correction, solid_coverage)
    domains = build_domains(cells, phases)
    solid_cells = tuple(sorted(domains))
    if not solid_cells:
        raise InputError("格网内没有可接受色样的实底格")
    phase_names = tuple(phase.name for phase in phases)
    groups = read_groups(data.get("groups", []), rows, columns, phase_names)
    bands = read_bands(data.get("bands", []), rows, columns, phase_names)
    problem = GridProblem(rows, columns, phase_names, domains, groups, bands)

    policy = choice(data.get("band_policy", "horizontal"), "试样带策略", BAND_POLICIES)
    outcome = adjudicate(problem, policy, node_budget=node_budget)
    default_name = outcome["default_family"]
    if default_name is None:
        raise InputError("没有能同时满足色样、同相连片和试样带的判读图，请放宽同相连片或改试样带策略")
    family = outcome["families"][default_name]
    requested = outcome["families"][policy]

    gates = [
        AreaGate.read(raw, phase_names, image.width, image.height)
        for raw in seq(data.get("area_gates", []), "面积放行条件", maximum=12)
    ]
    gate_reports = [
        _certify_gate(gate, problem, family, geometry, solid_cells, node_budget) for gate in gates
    ]
    all_certified = all(report["passed"] for report in gate_reports) if gate_reports else True
    all_proven = all(report["proven"] for report in gate_reports) if gate_reports else True
    release = {
        "status": "releasable" if all_certified and all_proven else (
            "inconclusive" if not all_proven else "held"),
        "certifying_family": default_name,
        "gates": gate_reports,
        "rule": "所有仍讲得通的整图判法都满足面积百分比才放行；单张工作图不够",
    }

    specimen = specimen_requirements(data.get("specimen_areas", []), geometry, phase_names,
                                     solid_cells, image.width, image.height)
    specimen_reports = []
    active_bands = tuple(band for band in problem.bands if band.name in family["active_bands"])
    for requirement in specimen:
        low = minimum_phase_area(problem, requirement["weights"], requirement["phase"],
                                 active_bands=active_bands, node_budget=node_budget)
        high = maximum_phase_area(problem, requirement["weights"], requirement["phase"],
                                  active_bands=active_bands, node_budget=node_budget)
        specimen_reports.append({
            "name": requirement["name"], "phase": requirement["phase"],
            "required_fraction": requirement["minimum_fraction"],
            "minimum_fraction": round(low["value"] / requirement["area"], 6) if low.get("feasible") else None,
            "maximum_fraction": round(high["value"] / requirement["area"], 6) if high.get("feasible") else None,
            "proven": bool(low.get("proven") and high.get("proven")),
            "passed": bool(low.get("feasible") and low.get("proven")
                           and low["value"] / requirement["area"] + 1e-12 >= requirement["minimum_fraction"]),
        })

    audit_polygon = frame_polygon(data.get("audit_frame"), image.width, image.height)
    audit_report = (_frame_report(audit_polygon, problem, family, geometry, solid_cells, phases, node_budget)
                    if audit_polygon is not None else None)

    cell_payload = []
    for row in range(rows):
        for column in range(columns):
            reading = cells[(row, column)]
            cell_payload.append({
                "row": row, "column": column, "address": f"{row + 1},{column + 1}",
                "solid": (row, column) in domains,
                "blank": (row, column) not in domains,
                "coverage_fraction": reading["coverage_fraction"],
                "lab": reading.get("lab"), "delta_e": reading.get("delta_e", {}),
                "candidates": reading.get("candidates", []),
            })
    result = {
        "schema": "kilnledger-grid-phases-v1",
        "region": region.as_json(),
        "grid": {"rows": rows, "columns": columns, "solid_coverage": solid_coverage},
        "phases": [
            {"name": phase.name, "limit": phase.limit, "marker": html_color(phase.marker),
             "swatch": phase.photo_color, "ref_lab": [round(float(v), 4) for v in phase.lab]}
            for phase in phases
        ],
        "cells": cell_payload,
        "groups": [
            {"name": group.name, "phase": group.phase, "open": group.open,
             "cells": [list(cell) for cell in group.cells]}
            for group in groups
        ],
        "bands": [
            {"name": band.name, "phase": band.phase, "orientation": band.orientation,
             "span": band.span, "center": band.center, "required": band.required,
             "cells": [list(cell) for cell in band.cells]}
            for band in bands
        ],
        "band_policy": policy,
        "default_family": default_name,
        "requested_family_feasible": requested["feasible"],
        "families": [
            {
                "policy": name,
                "feasible": details["feasible"],
                "proven": details["proven"],
                "solution_count": details["solution_count"],
                "search_nodes": details["search_nodes"],
                "active_bands": details["active_bands"],
                "relaxed_bands": details["relaxed_bands"],
                "example": _serialise_assignment(details["example"]),
                "detour": details["detour"],
            }
            for name, details in outcome["families"].items()
        ],
        "conflicts": outcome["conflicts"],
        "release": release,
        "specimen_areas": specimen_reports,
        "audit_frame": {"vertices": [list(point) for point in audit_polygon],
                        "phases": audit_report} if audit_polygon is not None else None,
    }
    diagram = render_diagram(geometry, cells, phases, family["example"], groups, bands,
                             gates, audit_polygon, default_name)
    return result, diagram


def _font(size=13):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render_diagram(geometry, cells, phases, assignment, groups, bands, gates, audit_polygon, family_name):
    rows, columns = geometry.rows, geometry.columns
    margin = 46
    cell_px = (DIAGRAM_SIDE - 2 * margin) // max(rows, columns)
    grid_width = cell_px * columns
    legend = ("frame: dark=group  orange/blue=band  purple=gate  green=archive   X=empty cell")
    width = max(margin * 2 + grid_width, margin * 2 + _font(12).getlength(legend))
    height = margin * 2 + cell_px * rows + 34
    image = Image.new("RGBA", (width, height), "#f6f1e6")
    draw = ImageDraw.Draw(image)
    markers = {phase.name: phase.marker for phase in phases}

    def origin(row, column):
        return margin + column * cell_px, margin + row * cell_px

    for row in range(rows):
        for column in range(columns):
            x0, y0 = origin(row, column)
            box = (x0, y0, x0 + cell_px, y0 + cell_px)
            reading = cells[(row, column)]
            phase_name = assignment.get((row, column)) if assignment else None
            if phase_name is not None:
                draw.rectangle(box, fill=markers[phase_name] + (205,))
            elif reading["solid"] and reading.get("candidates"):
                draw.rectangle(box, fill="#efe6d2")
            else:
                draw.rectangle(box, fill="#ded8cc")
                for offset in range(-cell_px, cell_px, 12):
                    draw.line((x0 + offset, y0 + cell_px, x0 + offset + cell_px, y0),
                              fill="#c9c2b4", width=1)
            draw.rectangle(box, outline="#7c7263", width=1)
            label = phase_name or ("?" if reading["solid"] and reading.get("candidates") else "X")
            fill = "#221d16" if phase_name else "#7a7164"
            draw.text((x0 + 6, y0 + 4), label, fill=fill, font=_font(max(11, cell_px // 5)))

    for band in bands:
        color = "#c2633d" if band.orientation == "horizontal" else "#3d7fa6"
        for row, column in band.cells:
            x0, y0 = origin(row, column)
            draw.rectangle((x0 + 2, y0 + 2, x0 + cell_px - 2, y0 + cell_px - 2),
                           outline=color, width=3)
    for group in groups:
        for row, column in group.cells:
            x0, y0 = origin(row, column)
            draw.rectangle((x0 + 4, y0 + 4, x0 + cell_px - 4, y0 + cell_px - 4),
                           outline="#2f2a22", width=2)
    for gate in gates:
        for row in range(rows):
            for column in range(columns):
                overlap = geometry.overlap(row, column, gate.polygon)
                if overlap > 1e-9:
                    x0, y0 = origin(row, column)
                    draw.rectangle((x0 + 7, y0 + 7, x0 + cell_px - 7, y0 + cell_px - 7),
                                   outline="#8a3f8f", width=2)
    if audit_polygon is not None:
        points = [
            (margin + (x - geometry.bounds.left) / geometry.cell_width * cell_px,
             margin + (y - geometry.bounds.top) / geometry.cell_height * cell_px)
            for x, y in audit_polygon
        ]
        draw.line(points + points[:1], fill="#3d6b46", width=3)
    draw.text((margin, 8), f"GRID PHASE DIAGRAM · band family: {family_name}",
              fill="#3a3328", font=_font(15))
    legend_y = height - 26
    draw.text((margin, legend_y), legend, fill="#5d5448", font=_font(12))
    return image
