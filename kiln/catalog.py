"""Find previously transcribed specimens and build traceable reference sheets."""

from collections import Counter
from dataclasses import dataclass
from itertools import groupby

from PIL import Image, ImageDraw

from .imaging import flatten
from .inputs import InputError, choice, distinct, flag, integer, key, obj, optional_text, seq
from .paper import Paper, export_paper, print_article
from .trays import Tray, cell_quality


@dataclass(frozen=True)
class View:
    tray_record: dict
    tray: Tray
    photo: dict
    cell: object

    @property
    def position(self):
        return (self.tray_record["id"], self.cell.address)

    def as_json(self):
        return {
            "label": self.cell.coupon, "state": self.cell.state,
            "address": self.cell.address, "row": self.cell.row, "column": self.cell.column,
            "tray_id": self.tray_record["id"], "tray_title": self.tray.name,
            "tray_revision": self.tray_record["revision"],
            "photo_id": self.photo["id"], "photo_title": self.photo["title"],
            "photo_sha256": self.photo["png_sha"], "photo_role": self.photo["role"],
            "note": self.cell.note, "polygon": self.tray.polygon(self.cell), "backlit": self.tray.backlit,
        }


def views(store, firing_id, tray_filter):
    records = store.entries(firing_id, kind="tray")
    if tray_filter and tray_filter not in {item["id"] for item in records}:
        raise InputError("所选托盘不属于当前窑次")
    for record in records:
        if tray_filter and tray_filter != record["id"]:
            continue
        photo = store.photo(record["photo_id"])
        dataset = Tray.read(record["payload"], photo["width"], photo["height"])
        for cell in dataset.cells:
            yield View(record, dataset, photo, cell)


def matching_views(store, firing_id, user):
    data = obj(user, "编号索引")
    query = optional_text(data.get("query"), "编号检索", 40).casefold()
    match = choice(data.get("match", "contains"), "编号匹配", ("contains", "exact"))
    state = choice(data.get("state", "identified"), "格子状态", ("all", "identified", "covered", "unread", "empty"))
    tray_id = key(data["tray"], "托盘") if data.get("tray") else None
    skip_backlit = flag(data.get("skip_backlit", False), "略过背光照片")
    result = []
    for item in views(store, firing_id, tray_id):
        if item.tray.backlit and skip_backlit:
            continue
        value = item.cell.coupon.casefold()
        if query and ((match == "exact" and query != value) or (match == "contains" and query not in value)):
            continue
        if state != "all" and state != item.cell.state:
            continue
        result.append(item)
    sort = lambda item: (item.cell.coupon.casefold(), item.tray.name.casefold(), item.cell.row, item.cell.column)
    return sorted(result, key=sort)


def view_group(items):
    key_name = lambda item: item.cell.coupon.casefold()
    groups = []
    for coupon, known in groupby(items, key_name):
        group = list(known)
        groups.append({
            "label": group[0].cell.coupon or None,
            "count": len(group),
            "views": [item.as_json() for item in group],
            "group": "transcribed" if coupon else "unidentified",
        })
    return groups


def lookup(store, firing_id, settings):
    data = obj(settings, "编号索引")
    page = integer(data.get("page", 1), "页码", low=1, high=1000)
    limit = integer(data.get("limit", 28), "每页格数", low=5, high=70)
    known = matching_views(store, firing_id, data)
    start = (page - 1) * limit
    batch = known[start:start + limit]
    return {
        "page": page, "limit": limit, "page_count": (len(known) + limit - 1) // limit,
        "total_views": len(known), "states": dict(Counter(item.cell.state for item in known)),
        "groups": view_group(batch),
        "unique_transcribed": len({item.cell.coupon.casefold() for item in known if item.cell.coupon}),
    }


def select_positions(store, firing_id, settings):
    picks = []
    for entry in seq(settings.get("views", []), "指定位置", maximum=24):
        parts = obj(entry, "指定位置")
        tray = key(parts.get("tray"), "托盘")
        addr = optional_text(parts.get("address"), "托盘格", 4)
        picks.append((tray, addr))
    distinct(picks, "指定托盘格")
    available = matching_views(store, firing_id, settings)
    if not picks:
        if not available:
            raise InputError("当前索引没有可制作图册的已抄录位置")
        if len(available) > 24:
            raise InputError("图册最多 24 个位置，请增加识别条件或指定位置")
        return available
    by_position = {item.position: item for item in available}
    if set(picks).difference(by_position):
        raise InputError("有托盘格不符合本轮索引条件")
    return [by_position[position] for position in picks]


def overview_sheet(items, load_image, paper, firing, monochrome):
    photos = {}
    details = []
    articles = []
    for item in items:
        asset = item.photo["id"]
        if asset not in photos:
            photos[asset] = load_image(item.photo)
        primal = photos[asset]
        thumbnail = item.tray.cell_visual(primal, item.cell, size=270)
        fields = item.as_json()
        quality = cell_quality(primal, item.tray, item.cell)
        info = ("背光" if fields["backlit"] else "正照") + f" · 实底 {quality['coverage']:.0%}"
        title = item.cell.coupon or f"未抄 {item.cell.address}"
        articles.append(print_article(
            ((thumbnail, fields["photo_title"]),),
            title,
            (f"{fields['tray_title']} · {fields['address']}", fields["photo_title"], info),
            flag=fields["note"], grayscale=monochrome,
        ))
        details.append({**fields, "quality": quality})
    output, page_count = export_paper(paper, articles, footer=f"釉样台 · {firing['name']} · 影像位置索引")
    return details, output, page_count


def catalog(store, firing, settings, load_image):
    data = obj(settings, "编号图册")
    paper = Paper.read(data.get("paper", {}), default_title="已抄录实物编号图册")
    mono = flag(data.get("grayscale", False), "灰阶图册")
    items = select_positions(store, firing["id"], data)
    details, document, count = overview_sheet(items, load_image, paper, firing, mono)
    result = {
        "format": "html", "paper": paper.as_json(), "pages": count,
        "count": len(items), "items": details,
    }
    return (
        result, document, sorted({item.photo["id"] for item in items}),
        sorted({(item.tray_record["id"], item.tray_record["revision"]) for item in items}),
    )


def micro_contact(store, firing_id, settings, load_photo):
    data = obj(settings, "编号检视图")
    picks = select_positions(store, firing_id, data)
    if len(picks) > 12:
        raise InputError("并排检视图最多显示 12 格，请缩小选取")
    width = integer(data.get("columns", 3), "并排列数", low=1, high=4)
    cell = 188
    panels = (len(picks) + width - 1) // width
    board = Image.new("RGB", (width * cell + 22, panels * cell + 35), "#e8dec9")
    draw = ImageDraw.Draw(board)
    draw.text((12, 9), "COUPON INDEX / KNOWN POSITIONS", fill="#3d3325")
    held = {}
    for index, item in enumerate(picks):
        row, col = divmod(index, width)
        left, top = 10 + col * cell, 30 + row * cell
        if item.photo["id"] not in held:
            held[item.photo["id"]] = load_photo(item.photo)
        view = item.tray.cell_visual(held[item.photo["id"]], item.cell, size=163)
        board.paste(flatten(view, (223, 214, 198)), (left + (cell - view.width) // 2, top))
        draw.text((left + 8, top + 169), f"#{index + 1}   /   {item.cell.address}", fill="#513d2e")
    return board, [item.as_json() for item in picks]
