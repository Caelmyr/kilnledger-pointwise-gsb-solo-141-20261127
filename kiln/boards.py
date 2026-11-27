"""Assemble comparable photo windows, annotated exhibits and image montages."""

from dataclasses import dataclass

from PIL import Image, ImageDraw

from .calibration import Correction, analyze, corrected_image, delta_e
from .geometry import Region
from .imaging import crop_region, flatten, thumbnail
from .inputs import InputError, choice, distinct, flag, in_project, key, obj, optional_text, seq, text
from .paper import Paper, export_paper, print_article


@dataclass(frozen=True)
class Exhibit:
    photo: dict
    image: object
    sample: dict
    region: object
    label: str
    caption: str
    group: str

    def facts(self):
        return {
            "photo_id": self.photo["id"], "photo_title": self.photo["title"], "photo_sha256": self.photo["png_sha"],
            "region": self.region, "label": self.label, "caption": self.caption, "group": self.group,
            "swatch": self.sample["swatch"], "lab": self.sample["lab"], "coverage_px": self.sample["coverage_px"],
            "calibrated": self.sample["calibrated"],
        }


def load_exhibits(firing_id, entries, store, read_photo, *, calibrated):
    photos = {}
    results = []
    for index, value in enumerate(seq(entries, "展板样片", minimum=1, maximum=20), 1):
        raw = obj(value, "展板样片")
        photo_id = key(raw.get("photo"), "展板照片")
        if photo_id not in photos:
            stored = in_project(store.photo(photo_id), firing_id, "展板照片")
            image = read_photo(stored)
            correction = store.latest(firing_id, photo_id, "calibration") if calibrated else None
            model = Correction.from_json(correction["payload"]["result"] if correction else None)
            photos[photo_id] = (stored, image, model)
        photo, image, model = photos[photo_id]
        region = Region.read(raw.get("region"), *image.size).as_json()
        label = text(raw.get("label", f"样片 {index}"), "样片名称", limit=55)
        caption = optional_text(raw.get("caption"), "图片说明", 160)
        group = optional_text(raw.get("group"), "对照组", 30)
        measurement = analyze(image, region, model, bins=8)
        crop, _ = crop_region(image, region, max_side=650)
        if model.samples:
            crop = corrected_image(crop, model)
        results.append(Exhibit(photo, crop, measurement, region, label, caption, group))
    distinct([result.label.casefold() for result in results], "展板样片名称")
    return results


def groups(exhibits):
    positions = {}
    for exhibit in exhibits:
        group = exhibit.group
        if group and group in positions:
            positions[group].append(exhibit)
        elif group:
            positions[group] = [exhibit]
    return positions


def pair_articles(exhibits, *, gray, cover):
    known = groups(exhibits)
    for name, pair in known.items():
        if len(pair) != 2:
            raise InputError(f"对照组「{name}」需要正好两张样片")
    consumed = set()
    articles = []
    for item in exhibits:
        if item.group:
            if item.group in consumed:
                continue
            left, right = known[item.group]
            consumed.add(item.group)
            difference = float(delta_e(left.sample["lab"], right.sample["lab"]))
            articles.append(print_article(
                ((left.image, left.photo["title"]), (right.image, right.photo["title"])),
                item.group,
                (f"{left.label} · {right.label}", f"ΔE76 {difference:.2f}"),
                grayscale=gray,
                cover=cover,
            ))
        else:
            articles.append(print_article(
                ((item.image, item.photo["title"]),), item.label,
                (item.caption, item.photo["title"], item.sample["swatch"]),
                grayscale=gray, cover=cover,
            ))
    return articles


def palette_strip(image, exhibits, names):
    draw = ImageDraw.Draw(image)
    x = 0
    for exhibit, width in zip(exhibits, names):
        draw.rectangle((x, 0, x + width - 1, image.height - 1), fill=exhibit.sample["swatch"])
        x += width
    return image


def collection_strip(exhibits, *, width, uniform):
    if uniform:
        basis = [1] * len(exhibits)
    else:
        basis = [item.sample["coverage_px"] for item in exhibits]
    boundaries = [0]
    cumulative = 0
    for value in basis:
        cumulative += value
        boundaries.append(round(width * cumulative / sum(basis)))
    increments = [b - a for a, b in zip(boundaries, boundaries[1:])]
    if min(increments) < 4:
        raise InputError("覆盖权重让某一色样宽度不足 4 像素")
    strip = palette_strip(Image.new("RGB", (width, 76)), exhibits, increments)
    return strip, [
        {"label": entry.label, "swatch": entry.sample["swatch"], "width": size}
        for entry, size in zip(exhibits, increments)
    ]


def board_artifact(store, read_photo, firing, data):
    paper = Paper.read(data.get("paper", {}), default_title="窑后呈色对照展板")
    grayscale = flag(data.get("grayscale", False), "灰阶展板")
    calibrated = flag(data.get("calibrated", True), "使用校色")
    fit = choice(data.get("fit", "contain"), "照片贴合", ("contain", "cover"))
    exhibits = load_exhibits(firing["id"], data.get("items"), store, read_photo, calibrated=calibrated)
    articles = pair_articles(exhibits, gray=grayscale, cover=fit == "cover")
    html, pages = export_paper(paper, articles, footer=f"釉样台 · {firing['name']} · 展板")
    source_ids = sorted({entry.photo["id"] for entry in exhibits})
    return {
        "layout": paper.as_json(), "pages": pages, "presentation_count": len(articles),
        "grayscale": grayscale, "fit": fit, "items": [entry.facts() for entry in exhibits],
    }, html, source_ids


def preview_grid(exhibits, columns=3, cell_size=180):
    columns = min(len(exhibits), columns)
    rows = (len(exhibits) + columns - 1) // columns
    result = Image.new("RGB", (columns * cell_size + 16, rows * (cell_size + 27) + 16), "#e4dace")
    draw = ImageDraw.Draw(result)
    for index, item in enumerate(exhibits):
        col, row = index % columns, index // columns
        left, top = 8 + col * cell_size, 8 + row * (cell_size + 27)
        view = thumbnail(item.image, cell_size - 12)
        result.paste(flatten(view, (239, 233, 220)), (left + (cell_size - view.width) // 2,
                                                       top + (cell_size - view.height) // 2))
        draw.rectangle((left + 6, top + cell_size + 2, left + cell_size - 6, top + cell_size + 15),
                       fill=item.sample["swatch"])
    return result
