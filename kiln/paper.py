"""Self-contained print layouts with fixed 100% physical scale."""

from base64 import b64encode
from dataclasses import dataclass
from html import escape
import math

from PIL import Image

from .imaging import jpeg_bytes
from .inputs import InputError, choice, integer, number, obj, optional_text


PAGE_FORMATS = {
    "a4": (210, 297),
    "letter": (215.9, 279.4),
    "a5": (148, 210),
}


@dataclass(frozen=True)
class Paper:
    format_name: str
    width: float
    height: float
    border: float
    gap: float
    columns: int
    row_size: float
    title: str
    caption: str

    @classmethod
    def read(cls, user, *, default_title, for_cards=False):
        raw = obj(user, "用纸")
        key = choice(raw.get("format", "a4"), "纸张尺寸", tuple(PAGE_FORMATS))
        orientation = choice(raw.get("orientation", "portrait"), "方向", ("portrait", "landscape"))
        width, height = PAGE_FORMATS[key]
        if orientation == "landscape":
            width, height = height, width
        border = number(raw.get("margin", 12), "页边空白", low=5, high=28)
        gap = number(raw.get("gap", 4), "图间空隙", low=2, high=15)
        columns = integer(raw.get("columns", 2), "每行列数", low=1, high=4)
        row_size = number(raw.get("row_size", 55), "卡片高度", low=37, high=108)
        title = optional_text(raw.get("title", default_title), "版面标题", 76) or default_title
        caption = optional_text(raw.get("caption"), "版面副题", 150)
        paper = cls(key, width, height, border, gap, columns, row_size, title, caption)
        if paper.cell_width < (46 if for_cards else 34):
            raise InputError("在选定的纸张和页边留白内，卡片太窄")
        if paper.rows_per_page < 1:
            raise InputError("一张纸留不下任何卡片")
        return paper

    @property
    def cell_width(self):
        return (self.width - 2 * self.border - (self.columns - 1) * self.gap) / self.columns

    @property
    def header_height(self):
        return 19 if self.caption else 15

    @property
    def rows_per_page(self):
        return math.floor((self.height - 2 * self.border - self.header_height - 10 + self.gap)
                          / (self.row_size + self.gap) + 1e-9)

    @property
    def capacity(self):
        return self.columns * self.rows_per_page

    def pages(self, items):
        return [items[index:index + self.capacity] for index in range(0, len(items), self.capacity)]

    def as_json(self):
        return {
            "format": self.format_name, "width_mm": self.width, "height_mm": self.height,
            "margin_mm": self.border, "gap_mm": self.gap, "columns": self.columns,
            "rows_per_page": self.rows_per_page, "row_size_mm": self.row_size,
            "cell_width_mm": round(self.cell_width, 4), "title": self.title, "caption": self.caption,
        }


def inline_picture(image, *, grayscale=False, side=640):
    copy = image.copy()
    copy.thumbnail((side, side), Image.Resampling.LANCZOS)
    if grayscale:
        gray = copy.convert("L")
        background = Image.new("L", gray.size, 246)
        if "A" in copy.getbands():
            background.paste(gray, mask=copy.getchannel("A"))
        else:
            background = gray
        copy = background.convert("RGB")
    else:
        copy = copy.convert("RGBA")
    data = jpeg_bytes(copy, quality=88)
    return "data:image/jpeg;base64," + b64encode(data).decode("ascii")


def paper_css(paper):
    return f"""
      :root {{ color-scheme: light; font-family: system-ui, sans-serif; background: #ded9cd; color: #272621 }}
      * {{ box-sizing: border-box }}
      body {{ margin: 0 }}
      .sheet {{ width: {paper.width}mm; height: {paper.height}mm; padding: {paper.border}mm;
         margin: 12mm auto; overflow: hidden; background: #fffcf4; break-after: page;
         display: flex; flex-direction: column; page-break-after: always }}
      header {{ flex-basis: {paper.header_height}mm; flex-shrink: 0; display: flex; justify-content: space-between;
         align-items: flex-start; gap: 5mm }}
      header h1 {{ font-size: 5mm; margin: 0; font-weight: 650 }}
      header p {{ font-size: 2.7mm; margin: 1mm 0 0; overflow-wrap: anywhere }}
      header aside {{ color: #675d46; font-size: 2.8mm; white-space: nowrap }}
      main {{ display: grid; grid-template-columns: repeat({paper.columns}, minmax(0, 1fr)); gap: {paper.gap}mm;
         grid-auto-rows: {paper.row_size}mm }}
      article {{ border: .25mm solid #a69b82; overflow: hidden; padding: 2mm; display: grid;
         grid-template-rows: minmax(0, 1fr) auto; gap: 1.5mm; min-width: 0 }}
      .media {{ background: #eee6d4; overflow:hidden; min-height: 0; display:grid }}
      .media img {{ min-height: 0; min-width: 0; width: 100%; height: 100%; object-fit: contain }}
      .media.cover img {{ object-fit: cover }}
      .media.dual {{ grid-template-columns: 1fr 1fr; gap: 1mm }}
      .data {{ display: flex; flex-direction: column; gap: .6mm; font-size: 2.7mm; overflow: hidden }}
      .data strong {{ font-size: 3.3mm; white-space: nowrap; text-overflow: ellipsis; overflow: hidden }}
      .data span {{ overflow: hidden; white-space: nowrap; text-overflow: ellipsis }}
      .flag {{ color: #735a23; font-size: 2.6mm }}
      footer {{ margin-top: auto; min-height: 6.5mm; display: flex; gap: 4mm; align-items: end;
         justify-content: space-between; font-size: 2.4mm; color: #5e533e }}
      .ruler {{ display: inline-block; width: 30mm; height: 2mm; border: .2mm solid #24211a;
        border-top: 0; vertical-align: bottom; margin-right: 1mm }}
      .sheet:last-child {{ break-after: auto; page-break-after: auto }}
      @page {{ size: {paper.width}mm {paper.height}mm; margin: 0 }}
      @media print {{
         :root {{ background: white }}
         .sheet {{ margin: 0; print-color-adjust: exact; -webkit-print-color-adjust: exact }}
      }}
    """


def print_article(photos, title, lines, *, flag="", grayscale=False, cover=False):
    classes = ["media"]
    if len(photos) == 2:
        classes.append("dual")
    if cover:
        classes.append("cover")
    media = []
    for image, alt in photos:
        media.append(f'<img src="{inline_picture(image, grayscale=grayscale)}" alt="{escape(alt, quote=True)}">')
    info = [f'<strong title="{escape(title, quote=True)}">{escape(title)}</strong>']
    for value in lines:
        if value:
            info.append(f'<span title="{escape(value, quote=True)}">{escape(value)}</span>')
    if flag:
        info.append(f'<span class="flag">{escape(flag)}</span>')
    return (
        '<article><div class="' + " ".join(classes) + '">' + "".join(media) +
        '</div><div class="data">' + "".join(info) + '</div></article>'
    )


def export_paper(paper, articles, *, footer, restricted=""):
    pages = paper.pages(articles)
    sections = []
    for index, page in enumerate(pages, 1):
        description = f"<p>{escape(paper.caption)}</p>" if paper.caption else ""
        body = "".join(page)
        sections.append(f"""
          <section class="sheet">
            <header><div><h1>{escape(paper.title)}</h1>{description}</div><aside>{index} / {len(pages)}</aside></header>
            <main>{body}</main>
            <footer><span>{escape(footer)}</span><span><span class="ruler"></span>30 mm</span></footer>
          </section>
        """)
    policy = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    document = f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{policy}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(paper.title)}</title><style>{paper_css(paper)}{restricted}</style>
<body>{"".join(sections)}</body></html>"""
    return document.encode("utf-8"), len(pages)
