"""Stand-alone checks for the multi-photo tray reconciliation solver."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kiln.reconcile import Reconcile, rotate_local  # noqa: E402
from kiln.inputs import Conflict  # noqa: E402


def cell(row, column, state="unread", coupon=""):
    return {"row": row, "column": column, "state": state, "coupon": coupon}


def tray_payload(name, rows, columns, cells):
    return {
        "name": name, "rows": rows, "columns": columns,
        "origin": [0, 0], "last_column": [columns * 100, 0], "last_row": [0, rows * 100],
        "cells": cells,
    }


def entry(entry_id, payload):
    return {"id": entry_id, "kind": "tray", "revision": 1, "photo_id": entry_id + "-p",
            "firing_id": "f1", "payload": payload}


def photo(photo_id, title):
    return {"id": photo_id, "title": title, "width": 1000, "height": 1000}


def build(views, rows=4, columns=4, clues=None, rotations=None):
    records, photos = {}, {}
    raw_views = []
    for index, (tray_id, payload) in enumerate(views):
        records[tray_id] = entry(tray_id, payload)
        photos[tray_id + "-p"] = photo(tray_id + "-p", f"照片{index}")
        raw_views.append({"tray": tray_id, "rotations": rotations[index] if rotations else [0, 90, 180, 270]})
    raw = {"name": "测试拼合", "rows": rows, "columns": columns, "views": raw_views,
           "clues": clues or []}
    return Reconcile(raw, records, photos)


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: 期望 {expected}，实际 {actual}")
    print(f"  ✓ {message}")


def test_rotation_math():
    print("test_rotation_math")
    # 2 rows x 3 columns, 90 degree turn: local (r,c) -> master (c, rows-1-r)
    assert_equal(rotate_local(0, 0, 2, 3, 90), (0, 1), "90° 左上角")
    assert_equal(rotate_local(1, 2, 2, 3, 90), (2, 0), "90° 右下角")
    assert_equal(rotate_local(0, 0, 2, 3, 180), (1, 2), "180° 左上角")
    assert_equal(rotate_local(1, 2, 2, 3, 180), (0, 0), "180° 右下角")
    assert_equal(rotate_local(0, 0, 2, 3, 270), (2, 0), "270° 左上角")
    assert_equal(rotate_local(1, 2, 2, 3, 270), (0, 1), "270° 右下角")


def test_overlapping_identified_match():
    print("test_overlapping_identified_match")
    # Two 2x2 windows of a 3x3 master; overlapping cell must carry the same coupon.
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "unread"),
    ])
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "identified", "K3"), cell(0, 1, "identified", "K4"),
        cell(1, 0, "unread"), cell(1, 1, "identified", "K5"),
    ])
    c = tray_payload("丙", 2, 2, [
        cell(0, 0, "identified", "K5"), cell(0, 1, "unread"),
        cell(1, 0, "unread"), cell(1, 1, "empty"),
    ])
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=3, columns=3,
                  rotations=[[0], [0], [0, 90, 180, 270]])
    result = model.solve()
    assert result["solution_count"] >= 1, "至少有一种摆法"
    # K3 must be pinned to one master cell in every layout.
    pinned = [item for item in result["cells"] if item["fixed_coupon"].upper() == "K3"]
    assert_equal(len(pinned), 1, "K3 固定在唯一总盘格")
    print(f"    可行摆法 {result['solution_count']} 种")


def test_conflicting_coupons_no_solution():
    print("test_conflicting_coupons_no_solution")
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "identified", "K9"), cell(0, 1, "identified", "K5"),
        cell(1, 0, "identified", "K6"), cell(1, 1, "identified", "K7"),
    ])
    c = tray_payload("丙", 1, 1, [cell(0, 0, "identified", "K8")])
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=2, columns=2,
                  rotations=[[0], [0], [0, 90, 180, 270]])
    # Same-size grids on a 2x2 master force full overlap; K1 vs K9 conflicts.
    try:
        model.solve()
    except Conflict:
        print("  ✓ 冲突编号导致无解并报错")
        return
    raise AssertionError("应当无解")


def test_empty_cannot_overlap_visible():
    print("test_empty_cannot_overlap_visible")
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    # A disjoint second window: its empty cell must never land on a visible cell.
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "empty"), cell(0, 1, "identified", "K7"),
        cell(1, 0, "identified", "K8"), cell(1, 1, "identified", "K9"),
    ])
    c = tray_payload("丙", 1, 1, [cell(0, 0, "identified", "K10")])
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=4, columns=4,
                  rotations=[[0], [0], [0, 90, 180, 270]])
    result = model.solve()
    # A cell that is empty in every layout can never carry a coupon in any layout.
    for item in result["cells"]:
        if item["empty_in_all"]:
            assert not item["possible_coupons"], "全摆法为空的格上不能出现已抄编号"
    assert any("empty" in item["possible_states"] for item in result["cells"]), "保留了空格观察"
    print("  ✓ 空格始终避开可见试片格")


def test_same_and_different_clues():
    print("test_same_and_different_clues")
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    # Same four coupons, rotated 180; the same-cell clue anchors the alignment.
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "identified", "K4"), cell(0, 1, "identified", "K3"),
        cell(1, 0, "identified", "K2"), cell(1, 1, "identified", "K1"),
    ])
    c = tray_payload("丙", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    clues = [
        {"kind": "same", "cells": [{"view": 0, "row": 0, "column": 0},
                                    {"view": 1, "row": 1, "column": 1}]},
        {"kind": "different", "cells": [{"view": 0, "row": 0, "column": 0},
                                        {"view": 1, "row": 0, "column": 0}]},
    ]
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=3, columns=3, clues=clues,
                  rotations=[[0], [0, 90, 180, 270], [0]])
    result = model.solve()
    assert result["solution_count"] >= 1
    # The same-cell clue is consistent; the different-cell clue forbids a 0-degree overlay.
    rotations = {p["rotation"] for layout in result["layouts"] for p in layout["placements"] if p["view"] == 1}
    assert 0 not in rotations, "异格线索排除了 0° 同向叠放"
    k1_cells = [item for item in result["cells"] if "K1" in item["possible_coupons"]]
    assert k1_cells, "K1 仍可识别"
    print(f"  ✓ 同格/异格线索共同约束，剩 {result['solution_count']} 种摆法")


def test_shared_count_clue():
    print("test_shared_count_clue")
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "identified", "K2"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "identified", "K3"), cell(0, 1, "identified", "K4"),
        cell(1, 0, "identified", "K5"), cell(1, 1, "identified", "K6"),
    ])
    c = tray_payload("丙", 1, 1, [cell(0, 0, "identified", "K7")])
    clues = [{"kind": "shared", "a": {"view": 0, "row": 0, "column": 0},
              "b": {"view": 1, "row": 0, "column": 0}, "count": 2}]
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=4, columns=4, clues=clues)
    result = model.solve()
    assert result["solution_count"] >= 1
    for layout in result["layouts"]:
        map_a = {m["address"] for m in layout["placements"][0]["mapping"]}
        map_b = {m["address"] for m in layout["placements"][1]["mapping"]}
        assert_equal(len(map_a & map_b), 2, "两照共见恰好两格")
        break
    print("  ✓ 共见格数线索生效")


def test_covered_stays_unknown():
    print("test_covered_stays_unknown")
    a = tray_payload("甲", 2, 2, [
        cell(0, 0, "identified", "K1"), cell(0, 1, "covered"),
        cell(1, 0, "identified", "K3"), cell(1, 1, "identified", "K4"),
    ])
    b = tray_payload("乙", 2, 2, [
        cell(0, 0, "unread"), cell(0, 1, "identified", "K8"),
        cell(1, 0, "identified", "K9"), cell(1, 1, "identified", "K10"),
    ])
    c = tray_payload("丙", 1, 1, [cell(0, 0, "identified", "K11")])
    model = build([("t1", a), ("t2", b), ("t3", c)], rows=4, columns=4)
    result = model.solve()
    covered = [item for item in result["cells"] if "covered" in item["possible_states"]]
    assert covered, "至少有一格保留遮挡可能"
    assert all(not item["fixed_coupon"] for item in covered), "遮挡格不产生固定编号"
    print("  ✓ 遮挡位置保持未知")


if __name__ == "__main__":
    test_rotation_math()
    test_overlapping_identified_match()
    test_conflicting_coupons_no_solution()
    test_empty_cannot_overlap_visible()
    test_same_and_different_clues()
    test_shared_count_clue()
    test_covered_stays_unknown()
    print("\n全部求解器测试通过")
