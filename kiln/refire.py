"""Two-window refire bags issued from a multi-photo tray reconciliation.

A bag holds exactly two physical coupons and one retention card with two crop
windows.  A pair of master cells can only be signed off when *every* surviving
layout agrees that:

1. the two cells are two distinct physical objects;
2. the bag's coupon-number set is the same (both cells have one fixed number).

Everything else stays pending.  Each crop window keeps its source photo and
source cell position.
"""

from dataclasses import dataclass
from itertools import combinations

from .inputs import Conflict, InputError, choice, integer, obj, seq, text
from .paper import Paper, export_paper, print_article
from .reconcile import Reconcile, ViewCell, rotate_local
from .trays import cell_code, cell_quality


STRATEGIES = ("glaze", "number", "balanced")
STRATEGY_LABELS = {"glaze": "釉面优先", "number": "逐格号证优先", "balanced": "兼顾"}


@dataclass(frozen=True)
class BagPick:
    row: int
    column: int

    @classmethod
    def read(cls, raw):
        value = obj(raw, "复烧袋格位")
        return cls(integer(value.get("row"), "行", high=5), integer(value.get("column"), "列", high=5))

class RefireBags:
    def __init__(self, store, open_photo):
        self.store = store
        self.open_photo = open_photo

    # ------------------------------------------------------------ evaluation
    def evaluate(self, reconcile: Reconcile, picks):
        if len(picks) != 2:
            raise InputError("一个复烧袋恰好放两枚试片")
        for pick in picks:
            if not (0 <= pick.row < reconcile.rows and 0 <= pick.column < reconcile.columns):
                raise InputError("所选格不在总盘内")
        if picks[0] == picks[1]:
            raise InputError("两个裁图窗必须来自不同的格")
        layouts = reconcile.solutions
        bag_sets, distinct_hits, merges = [], 0, 0
        for layout in layouts:
            occupied = {}
            for placement in layout:
                for ref, row, column in placement.cells:
                    occupied.setdefault((row, column), []).append(ref)
            numbers = set()
            resolved = []
            for pick in picks:
                refs = occupied.get((pick.row, pick.column))
                if not refs:
                    resolved.append(None)
                    continue
                coupons = {reconcile.observation(ref).coupon.casefold()
                           for ref in refs if reconcile.observation(ref).state == "identified"}
                resolved.append(coupons)
                numbers.update(coupons)
            pair = [item for item in resolved if item is not None]
            if len(pair) == 2 and pair[0].isdisjoint(pair[1]):
                distinct_hits += 1
            if len(pair) == 2 and not pair[0].isdisjoint(pair[1]):
                merges += 1
            bag_sets.append(tuple(sorted(numbers)))
        fixed = [self._fixed_number(reconcile, pick) for pick in picks]
        always_distinct = distinct_hits == len(layouts) and merges == 0
        same_bag_set = len(set(bag_sets)) == 1 and len(bag_sets[0]) == 2
        signable = always_distinct and same_bag_set and all(fixed)
        reasons = []
        if not always_distinct:
            reasons.append("存在摆法让两格对应同一枚实物或无法区分")
        if not same_bag_set:
            reasons.append("各摆法推出的整袋编号集合不一致")
        if not all(fixed):
            reasons.append("有格在可行摆法中编号不固定")
        return {
            "signable": signable,
            "reasons": reasons,
            "bag_numbers": sorted(bag_sets[0]) if signable else [],
            "group_name": "+".join(sorted(bag_sets[0])).upper() if signable else "",
            "cells": [self._cell_report(reconcile, pick, number)
                      for pick, number in zip(picks, fixed)],
        }

    @staticmethod
    def _fixed_number(reconcile, pick):
        for cell in reconcile.result()["cells"]:
            if cell["row"] == pick.row and cell["column"] == pick.column:
                return cell["fixed_coupon"].upper() or None
        return None

    def _cell_report(self, reconcile, pick, fixed_number):
        # Every (photo, local cell) that can land on this master cell, with quality.
        options = []
        for view in reconcile.views:
            tray, photo = view["tray"], view["photo"]
            image = self.open_photo(photo)
            for cell in tray.cells:
                if not self._cell_can_land(reconcile, view["index"], cell, pick):
                    continue
                quality = cell_quality(image, tray, cell)
                options.append({
                    "view": view["index"], "row": cell.row, "column": cell.column,
                    "address": cell.address, "state": cell.state, "coupon": cell.coupon,
                    "photo_id": photo["id"], "photo_title": photo["title"],
                    "tray_id": view["entry"]["id"], "tray_name": tray.name,
                    "has_number": cell.state == "identified",
                    "coverage": quality["coverage"], "sharpness": quality["sharpness"],
                    "ref": ViewCell(view["index"], cell.row, cell.column),
                })
        return {"row": pick.row, "column": pick.column, "address": cell_code(pick.row, pick.column),
                "fixed_coupon": fixed_number, "options": options}

    @staticmethod
    def _cell_can_land(reconcile, view_index, cell, pick):
        tray = reconcile.views[view_index]["tray"]
        for layout in reconcile.solutions:
            placement = layout[view_index]
            row, column = rotate_local(cell.row, cell.column, tray.rows, tray.columns, placement.rotation)
            if (placement.top + row, placement.left + column) == (pick.row, pick.column):
                return True
        return False

    # ------------------------------------------------------------ window pick
    @staticmethod
    def choose_windows(report, strategy):
        windows = []
        for cell in report["cells"]:
            choices = [option for option in cell["options"] if option["state"] != "covered"]
            if not choices:
                raise Conflict(f"{cell['address']}只有遮挡格可用，不能制卡")
            if strategy == "number":
                numbered = [option for option in choices if option["has_number"]]
                pool = numbered or choices
                ranked = sorted(pool, key=lambda o: (o["coverage"], o["sharpness"] or 0), reverse=True)
                degraded = not numbered
            elif strategy == "glaze":
                ranked = sorted(choices, key=lambda o: (o["coverage"], o["sharpness"] or 0), reverse=True)
                degraded = not ranked[0]["has_number"]
            else:
                glaze_best = max(choices, key=lambda o: (o["coverage"], o["sharpness"] or 0))
                numbered = [option for option in choices if option["has_number"]]
                if numbered:
                    number_best = max(numbered, key=lambda o: (o["coverage"], o["sharpness"] or 0))
                    ranked = [number_best] + [o for o in sorted(
                        choices, key=lambda o: (o["coverage"], o["sharpness"] or 0), reverse=True)
                        if o is not number_best]
                    degraded = number_best is not glaze_best
                else:
                    ranked = sorted(choices, key=lambda o: (o["coverage"], o["sharpness"] or 0), reverse=True)
                    degraded = True
            windows.append({"cell": cell, "option": ranked[0], "degraded": degraded,
                            "alternatives": ranked[1:]})
        return windows

    # ------------------------------------------------------------ card output
    def prepare(self, firing, reconcile, raw):
        envelope = obj(raw, "复烧袋留样卡请求")
        data = obj(envelope["复烧袋留样卡"], "复烧袋留样卡") if "复烧袋留样卡" in envelope else envelope
        title = text(data.get("title", "复烧袋双窗留样卡"), "制卡名目", limit=75)
        strategy = choice(data.get("strategy", "balanced"), "选窗策略", STRATEGIES)
        picks = [BagPick.read(value) for value in seq(data.get("picks", []), "复烧袋格位", minimum=2, maximum=2)]
        report = self.evaluate(reconcile, picks)
        if not report["signable"]:
            raise Conflict("此两格暂不可签发：" + "；".join(report["reasons"]))
        windows = self.choose_windows(report, strategy)
        paper = Paper.read(data.get("paper", {}), default_title=title, for_cards=True)
        images = {}
        articles = []
        records = []
        for window in windows:
            option = window["option"]
            photo = self.store.photo(option["photo_id"])
            image = images.get(photo["id"])
            if image is None:
                image = self.open_photo(photo)
                images[photo["id"]] = image
            view = reconcile.views[option["view"]]
            picture = view["tray"].cell_visual(image, view["tray"].cell(option["row"], option["column"]), size=315)
            lines = (
                f"{view['tray'].name} · 来源格 {option['address']} → 总盘 {window['cell']['address']}",
                option["photo_title"],
                f"编号 {window['cell']['fixed_coupon']} · 实底 {round(option['coverage'] * 100)}%"
                + (" · 此窗无逐格号证" if window["degraded"] else ""),
            )
            articles.append(print_article(
                ((picture, option["photo_title"]),), window["cell"]["fixed_coupon"], lines, flag="复烧签发"))
            records.append({
                "master_row": window["cell"]["row"], "master_column": window["cell"]["column"],
                "master_address": window["cell"]["address"], "coupon": window["cell"]["fixed_coupon"],
                "photo_id": option["photo_id"], "photo_title": option["photo_title"],
                "tray_id": option["tray_id"], "source_row": option["row"], "source_column": option["column"],
                "source_address": option["address"], "coverage": option["coverage"],
                "sharpness": option["sharpness"], "degraded": window["degraded"],
                "alternatives": [{"photo_id": alt["photo_id"], "photo_title": alt["photo_title"],
                                  "source_address": alt["address"], "has_number": alt["has_number"],
                                  "coverage": alt["coverage"], "sharpness": alt["sharpness"]}
                                 for alt in window["alternatives"]],
            })
        html, pages = export_paper(
            paper, articles,
            footer=f"釉样台 · {firing['name']} · 复烧袋 {report['group_name']} · {title}")
        payload = {
            "format": "html", "title": title, "strategy": strategy,
            "strategy_label": STRATEGY_LABELS[strategy], "group_name": report["group_name"],
            "bag_numbers": report["bag_numbers"], "pages": pages, "paper": paper.as_json(),
            "windows": records,
            "solution_count": reconcile.result()["solution_count"],
        }
        sources = sorted({record["photo_id"] for record in records})
        revisions = sorted({(view["entry"]["id"], view["entry"]["revision"]) for view in reconcile.views})
        return payload, html, sources, revisions

    def suggestions(self, reconcile, limit=6):
        """All signable pairs of master cells, ranked for the default strategy."""
        candidates = []
        for first, second in combinations(
                [(cell["row"], cell["column"]) for cell in reconcile.result()["cells"]], 2):
            try:
                report = self.evaluate(reconcile, [BagPick(*first), BagPick(*second)])
            except InputError:
                continue
            if not report["signable"]:
                continue
            windows = self.choose_windows(report, "balanced")
            glaze_score = sum(window["option"]["coverage"] for window in windows)
            number_kept = sum(not window["degraded"] for window in windows)
            candidates.append({
                "picks": [{"row": first[0], "column": first[1]}, {"row": second[0], "column": second[1]}],
                "group_name": report["group_name"], "bag_numbers": report["bag_numbers"],
                "glaze_score": round(glaze_score, 4), "windows_with_number": number_kept,
                "windows": [{"address": window["cell"]["address"],
                             "photo_title": window["option"]["photo_title"],
                             "source_address": window["option"]["address"],
                             "coverage": window["option"]["coverage"],
                             "has_number": window["option"]["has_number"],
                             "degraded": window["degraded"]} for window in windows],
            })
        candidates.sort(key=lambda item: (item["windows_with_number"], item["glaze_score"]), reverse=True)
        return candidates[:limit]
