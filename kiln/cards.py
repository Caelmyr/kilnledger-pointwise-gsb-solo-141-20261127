"""Pick visible coupon observations and issue physical specimen cards."""

from dataclasses import dataclass

from .inputs import Conflict, InputError, choice, distinct, integer, key, obj, optional_text, revision, seq, text
from .paper import Paper, export_paper, print_article
from .trays import Tray, cell_quality


PURPOSES = ("refire", "retention", "review")


@dataclass(frozen=True)
class Pick:
    tray_id: str
    tray_revision: int
    row: int
    column: int
    purpose: str
    inscription: str
    minimum_coverage: float = 0.7

    @classmethod
    def read(cls, value):
        raw = obj(value, "留样卡位置")
        return cls(
            key(raw.get("tray"), "托盘来源"),
            revision(raw.get("revision")),
            integer(raw.get("row"), "行", high=11),
            integer(raw.get("column"), "列", high=11),
            choice(raw.get("purpose", "retention"), "保留目的", PURPOSES),
            optional_text(raw.get("inscription"), "卡片备注", 100),
        )

    @property
    def position(self):
        return self.tray_id, self.row, self.column


@dataclass(frozen=True)
class Observation:
    pick: Pick
    record: dict
    photo: dict
    tray: Tray
    cell: object
    quality: dict
    image: object

    @property
    def label(self):
        return self.cell.coupon

    @property
    def confirmed(self):
        return self.cell.state == "identified"

    def card_data(self):
        return {
            "photo_id": self.photo["id"], "photo_title": self.photo["title"],
            "photo_sha256": self.photo["png_sha"], "tray_id": self.record["id"],
            "tray_revision": self.record["revision"], "tray_name": self.tray.name,
            "address": self.cell.address, "row": self.cell.row, "column": self.cell.column,
            "polygon": self.tray.polygon(self.cell), "coupon": self.cell.coupon,
            "state": self.cell.state, "purpose": self.pick.purpose, "note": self.pick.inscription,
            "quality": self.quality,
        }


class Cards:
    def __init__(self, store, open_photo):
        self.store = store
        self.open_photo = open_photo

    def observation(self, firing, pick, images):
        tray_record = self.store.entry(pick.tray_id)
        if tray_record["kind"] != "tray" or tray_record["firing_id"] != firing["id"]:
            raise InputError("卡片必须使用当前窑次的托盘")
        if tray_record["revision"] != pick.tray_revision:
            raise Conflict("选择的托盘已更新，请刷新后重新确认卡片")
        photo = self.store.photo(tray_record["photo_id"])
        tray = Tray.read(tray_record["payload"], photo["width"], photo["height"])
        cell = tray.cell(pick.row, pick.column)
        image = images.get(photo["id"])
        if image is None:
            image = self.open_photo(photo)
            images[photo["id"]] = image
        quality = cell_quality(image, tray, cell)
        return Observation(pick, tray_record, photo, tray, cell, quality, image)

    @staticmethod
    def validate(observations, edition):
        labels = set()
        for item in observations:
            position = f"{item.tray.name} {item.cell.address}"
            if item.cell.state in ("empty", "covered"):
                raise Conflict(f"{position}已标记为空格或遮挡，无法制卡")
            if edition == "issue" and not item.confirmed:
                raise Conflict(f"{position}尚未抄录编号，仅可制作待核样卡")
            if item.quality["coverage"] < item.pick.minimum_coverage:
                raise Conflict(f"{position}的照片实底不足")
            label = item.label.casefold()
            if label and label in labels:
                raise Conflict(f"{item.label}已在同套卡里，实物编号只可签发一次")
            if label:
                labels.add(label)

    @staticmethod
    def article(observation, edition):
        item = observation
        picture = item.tray.cell_visual(item.image, item.cell, size=315)
        title = item.label or f"待核 {item.cell.address}"
        purposes = {"refire": "返烧对照", "retention": "留样", "review": "复查"}
        lines = (
            f"{item.tray.name} · {item.cell.address} · {purposes[item.pick.purpose]}",
            item.photo["title"],
            item.pick.inscription,
        )
        flag = "已签发" if edition == "issue" else "待核"
        return print_article(((picture, item.photo["title"]),), title, lines, flag=flag)

    def prepare(self, firing, data):
        title = text(data.get("title", "釉样实物留样卡"), "制卡名目", limit=75)
        edition = choice(data.get("edition", "review"), "制卡类型", ("review", "issue"))
        picks = [Pick.read(raw) for raw in seq(data.get("picks"), "留样卡位置", minimum=1, maximum=26)]
        distinct([pick.position for pick in picks], "留样卡位置")
        paper = Paper.read(data.get("paper", {}), default_title=title, for_cards=True)
        images = {}
        observations = [self.observation(firing, pick, images) for pick in picks]
        self.validate(observations, edition)
        articles = [self.article(item, edition) for item in observations]
        html, pages = export_paper(paper, articles, footer=f"釉样台 · {firing['name']} · {title}")
        records = [item.card_data() for item in observations]
        sources = sorted({item.photo["id"] for item in observations})
        revisions = sorted({(item.record["id"], item.record["revision"]) for item in observations})
        return {
            "edition": edition, "title": title, "paper": paper.as_json(), "pages": pages,
            "cards": records, "source_hashes": sorted({item["photo_sha256"] for item in records}),
        }, html, sources, revisions
