"""Application services for evidence analysis, image previews and archival."""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from PIL import Image

from . import (
    archive, boards, calibration, catalog, closeout, comparison, cracks, crazing, defects, detail,
    formulations, layer, recipe, rectify, relief, restoration, samples, series, surface, target,
)
from .cards import Cards
from .geometry import Ruler
from .imaging import decode_bytes, open_normalized, png_bytes, read_upload, thumbnail
from .inputs import (
    InputError, choice, coordinate, filename, flag, in_project, integer, key,
    obj, optional_text, revision, text,
)
from .store import Store
from .trays import Tray, cell_quality, contact_sheet


PHOTO_ROLES = ("overview", "detail", "reference", "repeat")
ANALYSES = {
    "calibration": "校色参考",
    "ruler": "标尺",
    "color": "样区读色",
    "pinholes": "针孔检查",
    "glare": "高光识别",
    "texture": "质地分布",
    "line": "测线",
    "traverse": "折线读色",
    "coverage": "实底遮挡",
    "crop": "细部裁片",
    "marks": "缺陷图",
    "spot": "斑点读色",
    "grades": "样区评级",
    "cells": "釉色一致性",
    "mask": "遮挡编辑",
    "cutout": "底板透底",
    "recolor": "高光减弱",
    "comparison": "共视差图",
    "overlay": "两片叠照",
    "palette": "样片调色条",
    "recipe": "返烧配方阵列",
    "crazing": "开片方向检查",
    "cracks": "裂纹归并",
    "phases": "釉相分布",
    "rectify": "试片校直",
    "relief": "双向掠射图",
    "target": "采集参考检查",
    "layer": "剖面厚度",
}
NAMES = {
    "tray": "托盘抄录", "cards": "留样卡", "board": "对照展板", "packet": "物证封存",
    "catalog": "已抄录编号图册", "closeout": "留档检核单", "selection": "保存的影像位置",
    **ANALYSES,
}


@dataclass(frozen=True)
class Analysis:
    kind: str
    source: dict
    settings: dict
    output: dict
    picture: Image.Image | None
    support: tuple = ()
    references: tuple = ()
    models: dict | None = None

    def payload(self):
        data = {
            "result": self.output,
            "source": {
                "photo_id": self.source["id"], "photo_title": self.source["title"],
                "photo_sha256": self.source["png_sha"],
            },
            "settings": self.settings,
            "models": self.models or {},
        }
        if self.picture is not None:
            data["format"] = "png"
        return data


def firing_fields(value):
    data = obj(value, "窑次目录")
    when = optional_text(data.get("fired_on"), "出窑日期", 10)
    if when:
        try:
            normalized = date.fromisoformat(when).isoformat()
        except ValueError:
            raise InputError("出窑日期应为 YYYY-MM-DD") from None
        if when != normalized:
            raise InputError("出窑日期应为 YYYY-MM-DD")
    return {
        "name": text(data.get("name"), "窑次名称", limit=84),
        "kiln": optional_text(data.get("kiln"), "窑炉编号", 50),
        "fired_on": when,
        "notes": optional_text(data.get("notes"), "窑次说明", 800),
    }


def photo_fields(value, *, title=None):
    data = obj(value, "照片信息")
    return {
        "title": text(data.get("title") or title, "照片名", limit=84),
        "role": choice(data.get("role", "overview"), "照片角色", PHOTO_ROLES),
        "note": optional_text(data.get("note"), "照片说明", 460),
    }


def source_model(entry):
    return {"id": entry["id"], "revision": entry["revision"], "kind": entry["kind"]} if entry else None


class Workbench:
    def __init__(self, root, demo):
        self.store = Store(root)
        self.demonstration = Path(demo)
        self.cards = Cards(self.store, self.open_photo)

    def open_photo(self, record):
        return open_normalized(self.store.require_blob(record["png_sha"]))

    def attach_decoded(self, firing_id, decoded, name, fields):
        kw = {
            **fields,
            "attachment_name": filename(name),
            "original_format": decoded.format,
            "source_sha": decoded.source_hash,
            "png_sha": self.store.put_blob(png_bytes(decoded.image)),
            "preview_sha": self.store.put_blob(png_bytes(thumbnail(decoded.image, size=480))),
            "width": decoded.width,
            "height": decoded.height,
            "had_profile": int(decoded.had_profile),
            "exif_oriented": int(decoded.exif_oriented),
        }
        return self.store.create_photo(firing_id, kw)

    def upload(self, firing_id, resource, data):
        self.store.firing(firing_id)
        decoded = read_upload(resource.stream)
        stem = Path(resource.filename or "新照片").stem
        fields = photo_fields(data, title=stem[:84])
        return self.attach_decoded(firing_id, decoded, resource.filename, fields)

    def photo_and_image(self, photo_id):
        source = self.store.photo(photo_id)
        return source, self.open_photo(source)

    def history(self, firing_id, args):
        role = args.get("role")
        if role:
            choice(role, "照片角色", PHOTO_ROLES)
        kind = args.get("kind")
        if kind:
            choice(kind, "记录类型", tuple(NAMES))
        search = args.get("q")
        if search:
            search = optional_text(search, "搜索词", 74)
        return {
            "firing": self.store.firing(firing_id),
            "photos": self.store.photos(firing_id, role=role, search=search),
            "entries": self.store.entries(firing_id, kind=kind, photo_id=args.get("photo")),
        }

    def ruler(self, photo):
        entry = self.store.latest(photo["firing_id"], photo["id"], "ruler")
        ruler = Ruler.read(entry["payload"]["result"], photo["width"], photo["height"]) if entry else None
        return ruler, entry

    def correction(self, photo, use=True):
        if not use:
            return calibration.Correction.identity(), None
        entry = self.store.latest(photo["firing_id"], photo["id"], "calibration")
        return calibration.Correction.from_json(entry["payload"]["result"] if entry else None), entry

    def analyze(self, photo_id, kind, user):
        settings = obj(user, "图像分析参数")
        photo, image = self.photo_and_image(photo_id)
        if kind not in ANALYSES:
            raise InputError("未识别的图像工具")
        use_calibration = flag(settings.get("calibrated", True), "使用校色")
        correction, active_color = self.correction(photo, use_calibration)
        ruler, active_ruler = self.ruler(photo)
        models = {}
        support = []
        for label, value in (("calibration", active_color), ("ruler", active_ruler)):
            if value is not None:
                support.append((value["id"], value["revision"]))
                models[label] = source_model(value)
        selection_id = settings.get("selection")
        if selection_id:
            bookmark = self.store.entry(key(selection_id, "保留位置"))
            settings = samples.select_for_tool(bookmark, photo, settings, kind)
            support.append((bookmark["id"], bookmark["revision"]))
            models["selection"] = source_model(bookmark)
        region = settings.get("region")
        extra_sources = []
        picture = None

        if kind == "calibration":
            result = calibration.fit_correction(image, settings.get("references")).to_json()
            picture = calibration.corrected_image(image, calibration.Correction.from_json(result))
        elif kind == "ruler":
            result = Ruler.read(settings, *image.size).to_json()
        elif kind == "color":
            bins = integer(settings.get("bins", 24), "直方图档数", low=8, high=64)
            result = calibration.analyze(image, region, correction, bins=bins)
            picture = calibration.histogram_image(result)
        elif kind == "pinholes":
            result, picture = surface.mark_pinholes(image, region, settings, correction, ruler.per_pixel if ruler else None)
        elif kind == "glare":
            result, picture = surface.mark_glare(image, region, settings, correction)
        elif kind == "texture":
            result, picture = surface.texture(image, region, settings, correction)
        elif kind == "line":
            result = surface.line_measure(image, settings, correction, ruler)
        elif kind == "traverse":
            result, picture = detail.traverse(image, settings, correction, ruler)
        elif kind == "coverage":
            result, picture = surface.aperture_check(image, region, settings)
        elif kind == "crop":
            if flag(settings.get("apply_calibration", False), "裁片使用校色"):
                image = calibration.corrected_image(image, correction)
            result, picture = detail.trim_sample(image, settings, ruler)
        elif kind == "marks":
            result, picture = defects.marks_result(image, settings, ruler)
        elif kind == "spot":
            result, picture = defects.spot_reading(image, settings, correction)
        elif kind == "grades":
            result = defects.grades(image, settings, correction)
        elif kind == "cells":
            result, picture = detail.cell_measurements(image, settings, correction)
        elif kind == "mask":
            result, picture = restoration.remove_surroundings(image, settings)
        elif kind == "cutout":
            result, picture = restoration.material_mask(image, settings, correction)
        elif kind == "recolor":
            result, picture = restoration.tame_glare(image, settings, correction)
        elif kind == "crazing":
            result, picture = crazing.inspect(image, settings, correction, ruler)
            if flag(settings.get("chart", False), "查看方向图"):
                picture = crazing.directionality_picture(result)
        elif kind == "cracks":
            marks_payload = None
            marks_id = settings.get("source_marks")
            if marks_id:
                record = self.store.entry(key(marks_id, "缺陷图记录"))
                if record["kind"] != "marks" or record["photo_id"] != photo["id"]:
                    raise InputError("引用的缺陷图记录需要是当前照片上的缺陷图")
                support.append((record["id"], record["revision"]))
                models["marks"] = source_model(record)
                marks_payload = record["payload"]
            result, picture = cracks.consolidate(image, settings, ruler, marks_payload)
        elif kind == "phases":
            result, picture = formulations.inspect(image, settings, correction, ruler)
            if flag(settings.get("chart", False), "查看面积柱图"):
                picture = formulations.distribution_plot(result)
        elif kind == "rectify":
            result, picture = rectify.straighten(image, settings)
        elif kind == "target":
            result, picture = target.check_targets(image, settings, correction)
            if flag(settings.get("chart", False), "查看色片区间"):
                picture = target.target_plot(result)
        elif kind == "layer":
            result, picture = layer.examine(image, settings, correction, ruler)
        elif kind == "recipe":
            result, picture, extra_sources = recipe.matrix(photo, image, correction, settings, self.store, self.open_photo)
        elif kind in ("comparison", "overlay", "relief"):
            other = in_project(self.store.photo(key(settings.get("comparison"), "对照照片")),
                               photo["firing_id"], "对照照片")
            other_image = self.open_photo(other)
            if kind != "overlay":
                other_model, other_reference = self.correction(other, use_calibration)
                if other_reference:
                    support.append((other_reference["id"], other_reference["revision"]))
                    models["comparison_calibration"] = source_model(other_reference)
                if kind == "relief":
                    result, picture = relief.surface_map(image, other_image, correction, other_model, settings)
                    if flag(settings.get("legend", False), "查看浮凸色标"):
                        picture = relief.preview_scale(result)
                else:
                    result, picture = comparison.compare(image, other_image, correction, other_model, settings)
            else:
                result, picture = comparison.blend_pair(image, other_image, settings)
            result["comparison_id"] = other["id"]
            result["comparison_sha256"] = other["png_sha"]
            extra_sources.append(other["id"])
        else:
            if settings.get("samples") is not None:
                result = {"swatches": calibration.palette_roles(image, settings.get("samples"), correction)}
            else:
                exhibits = boards.load_exhibits(photo["firing_id"], settings.get("items"), self.store,
                                                self.open_photo, calibrated=use_calibration)
                width = integer(settings.get("width", 720), "色条宽度", low=300, high=1800)
                uniform = flag(settings.get("uniform", True), "等宽色样")
                picture, strips = boards.collection_strip(exhibits, width=width, uniform=uniform)
                extra_sources = [exhibit.photo["id"] for exhibit in exhibits]
                result = {"swatches": strips, "source_samples": [exhibit.facts() for exhibit in exhibits]}
                if flag(settings.get("contact", False), "输出带色签的接触片"):
                    picture = boards.preview_grid(exhibits)
        return Analysis(kind, photo, settings, result, picture, tuple(support), tuple(extra_sources), models)

    def archive_analysis(self, analysis, title):
        fingerprint = self.store.put_blob(png_bytes(analysis.picture)) if analysis.picture is not None else None
        return self.store.create_entry(
            analysis.source["firing_id"],
            analysis.kind,
            title,
            analysis.payload(),
            photo_id=analysis.source["id"],
            artifact=fingerprint,
            expected_sources=analysis.support,
            references=analysis.references,
        )

    def save_result(self, photo_id, kind, user):
        body = obj(user, "保存图像分析")
        title = optional_text(body.get("title"), "记录名称", 90) or ANALYSES.get(kind, kind)
        analysis = self.analyze(photo_id, kind, body.get("settings", {}))
        return self.archive_analysis(analysis, title)

    def save_selection(self, photo_id, settings):
        photo, image = self.photo_and_image(photo_id)
        ruler, reference = self.ruler(photo)
        payload, picture = samples.make(photo, image, settings, ruler, reference)
        artifact = self.store.put_blob(png_bytes(picture))
        expected = [(reference["id"], reference["revision"])] if reference else ()
        return self.store.create_entry(
            photo["firing_id"], "selection", payload["name"], payload, photo_id=photo_id,
            artifact=artifact, expected_sources=expected,
        )

    def save_tray(self, photo_id, user):
        data = obj(user, "托盘抄录")
        photo = self.store.photo(photo_id)
        tray = Tray.read(data, photo["width"], photo["height"])
        payload = tray.to_json()
        existing = self.store.latest(photo["firing_id"], photo["id"], "tray")
        if existing:
            return self.store.replace_entry(existing["id"], revision(data.get("revision")), tray.name, payload)
        return self.store.create_entry(photo["firing_id"], "tray", tray.name, payload,
                                       photo_id=photo_id, unique_key=f"tray:{photo_id}")

    def tray(self, entry_id):
        entry = self.store.entry(entry_id)
        if entry["kind"] != "tray":
            raise InputError("该条目不是托盘")
        source, image = self.photo_and_image(entry["photo_id"])
        tray = Tray.read(entry["payload"], source["width"], source["height"])
        return entry, source, image, tray

    def transect_cell(self, tray_id, row, column, user):
        data = obj(user, "托盘格更新")
        record, _, _, tray = self.tray(tray_id)
        changed = tray.with_cell(row, column, data)
        return self.store.replace_entry(record["id"], revision(data.get("revision")), changed.name, changed.to_json())

    def inspect_cell(self, tray_id, user):
        data = obj(user, "托盘格位置")
        record, source, image, tray = self.tray(tray_id)
        cell = tray.locate(coordinate(data["point"], "原图点位")) if "point" in data else tray.cell(
            integer(data.get("row"), "行", high=11), integer(data.get("column"), "列", high=11))
        if cell is None:
            raise InputError("此点不在托盘内")
        quality = cell_quality(image, tray, cell)
        return {
            **cell.as_json(), "polygon": tray.polygon(cell), "quality": quality,
            "photo_id": source["id"], "tray_id": tray_id, "tray_revision": record["revision"],
        }, tray.cell_visual(image, cell, size=318)

    def view_tray(self, tray_id):
        _, _, image, tray = self.tray(tray_id)
        return contact_sheet(image, tray)

    def cards_artifact(self, firing_id, user, save):
        firing = self.store.firing(firing_id)
        data = obj(user, "留样卡")
        payload, content, references, versions = self.cards.prepare(firing, data)
        payload["format"] = "html"
        result = {"result": payload, "html": content.decode("utf-8")}
        if not save:
            return result
        entry = self.store.create_entry(
            firing_id, "cards", payload["title"], payload, artifact=self.store.put_blob(content),
            references=references, expected_sources=versions,
        )
        return {"entry": entry, **result}

    def make_board(self, firing_id, user, save):
        firing = self.store.firing(firing_id)
        data = obj(user, "对照展板")
        payload, content, references = boards.board_artifact(self.store, self.open_photo, firing, data)
        payload["format"] = "html"
        result = {"result": payload, "html": content.decode("utf-8")}
        if not save:
            return result
        entry = self.store.create_entry(
            firing_id, "board", payload["layout"]["title"], payload,
            artifact=self.store.put_blob(content), references=references,
        )
        return {"entry": entry, **result}

    def search_catalog(self, firing_id, settings):
        self.store.firing(firing_id)
        return catalog.lookup(self.store, firing_id, settings)

    def catalog_pictures(self, firing_id, settings):
        self.store.firing(firing_id)
        return catalog.micro_contact(self.store, firing_id, settings, self.open_photo)

    def print_catalog(self, firing_id, settings, save):
        firing = self.store.firing(firing_id)
        payload, document, sources, versions = catalog.catalog(self.store, firing, settings, self.open_photo)
        result = {"result": payload, "html": document.decode("utf-8")}
        if save:
            result["entry"] = self.store.create_entry(
                firing_id, "catalog", payload["paper"]["title"], payload,
                artifact=self.store.put_blob(document), references=sources, expected_sources=versions,
            )
        return result

    def closeout(self, firing_id, settings, save):
        firing = self.store.firing(firing_id)
        payload, document, sources, versions = closeout.closeout(self.store, firing, settings, self.open_photo)
        result = {"result": payload, "html": document.decode("utf-8")}
        if save:
            result["entry"] = self.store.create_entry(
                firing_id, "closeout", payload["title"], payload, artifact=self.store.put_blob(document),
                references=sources, expected_sources=versions,
            )
        return result

    def trends(self, settings):
        return series.build_series(self.store, settings)

    def packet(self, firing_id, settings):
        current = self.store.firing(firing_id)
        result, content, photos, expected = archive.make_packet(self.store, current, settings)
        return self.store.create_entry(
            firing_id, "packet", result["title"], {**result, "format": "zip"},
            artifact=self.store.put_blob(content), references=photos, expected_sources=expected,
        )

    def seed(self):
        existing = self.store.list_firings()
        if existing:
            return None
        return self.add_demo()

    def add_demo(self):
        definition = json.loads((self.demonstration / "index.json").read_text(encoding="utf-8"))
        current = self.store.create_firing(firing_fields(definition["firing"]))
        known_photos = {}
        for item in definition["photos"]:
            path = self.demonstration / item["asset"]
            decoded = decode_bytes(path.read_bytes())
            known_photos[item["asset"]] = self.attach_decoded(
                current["id"], decoded, path.name, photo_fields(item))
        for item in definition["trays"]:
            photo = known_photos[item["asset"]]
            self.save_tray(photo["id"], item["tray"])
        for item in definition.get("records", []):
            photo = known_photos[item["asset"]]
            self.save_result(photo["id"], item["kind"], {"settings": item["settings"], "title": item["title"]})
        return self.history(current["id"], {})
