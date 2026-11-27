"""Build a firing disposition sheet from selected archived image readings."""

from dataclasses import dataclass

from PIL import Image

from .archive import csv_bytes
from .inputs import InputError, choice, distinct, flag, in_project, key, number, obj, optional_text, seq, text
from .paper import Paper, export_paper, print_article


CHECKS = {
    "color_spread": ("color", "色差散布", "maximum", 8),
    "pinhole_count": ("pinholes", "针孔数", "maximum", 12),
    "glare_usable": ("glare", "高光面积占比", "maximum", 0.08),
    "sample_coverage": ("coverage", "样区实底分数", "minimum", 0.82),
    "patches_pass": ("target", "采集参考警报", "maximum", 0),
    "crazing_length": ("crazing", "开片示意线长度（像素）", "maximum", 80),
    "phase_confidence": ("phases", "明确釉相占比", "minimum", 0.75),
}


@dataclass(frozen=True)
class Rule:
    id: str
    target: str
    threshold: float
    required: bool
    entry_id: str | None
    comment: str

    @classmethod
    def read(cls, raw):
        data = obj(raw, "留档检查项")
        ident = key(data.get("id"), "检查项代号")
        target = choice(data.get("target"), "测量指标", tuple(CHECKS))
        threshold = number(data.get("threshold", CHECKS[target][3]), "检查阈值", low=0, high=400000)
        if target in ("glare_usable", "sample_coverage", "phase_confidence") and threshold > 1:
            raise InputError("面积占比的检查阈值应介于 0 和 1")
        required = flag(data.get("required", True), "必查")
        eid = key(data.get("entry"), "指定测量记录") if data.get("entry") else None
        return cls(ident, target, threshold, required, eid, optional_text(data.get("comment"), "检查备注", 150))

    @property
    def kind(self):
        return CHECKS[self.target][0]

    @property
    def name(self):
        return CHECKS[self.target][1]

    @property
    def expectation(self):
        return CHECKS[self.target][2]

    def summary(self):
        return {
            "id": self.id, "target": self.target, "name": self.name, "required": self.required,
            "threshold": self.threshold, "expectation": self.expectation, "comment": self.comment,
        }

    def measure(self, source):
        values = source["payload"].get("result", {})
        if self.target == "color_spread":
            value = values.get("delta_e_spread", {}).get("p90")
        elif self.target == "pinhole_count":
            value = values.get("count")
        elif self.target == "glare_usable":
            value = values.get("fraction")
        elif self.target == "patches_pass":
            value = values.get("warning_count")
            if not values.get("passed") and value == 0:
                value = 1
        elif self.target == "crazing_length":
            value = values.get("centerline_px")
        elif self.target == "phase_confidence":
            unclassified = values.get("unclassified_fraction", 1)
            ambiguous = values.get("ambiguous_fraction", 0)
            value = max(0, 1 - unclassified - ambiguous)
        else:
            if values.get("geometric_area"):
                value = values["covered_area"] / values["geometric_area"]
            else:
                value = None
        if value is None:
            return None, "missing_measure"
        passed = value <= self.threshold if self.expectation == "maximum" else value >= self.threshold
        return float(value), "supported" if passed else "outside"


def resolve(store, firing_id, requirements):
    versions = []
    photos = []
    checks = []
    selected = []
    for item in requirements:
        evidence = None
        if item.entry_id:
            evidence = in_project(store.entry(item.entry_id), firing_id, "归档测量")
            if evidence["kind"] != item.kind:
                raise InputError(f"{item.name}需要对应的测量记录")
        if evidence:
            value, state = item.measure(evidence)
            versions.append((evidence["id"], evidence["revision"]))
            photos.append(evidence["photo_id"])
            source = {"id": evidence["id"], "title": evidence["title"], "revision": evidence["revision"],
                      "photo_id": evidence["photo_id"]}
        else:
            value, state, source = None, "missing", None
        checks.append({
            **item.summary(), "entry": source, "status": state, "value": round(value, 6) if value is not None else None,
        })
        selected.append(evidence)
    return checks, selected, sorted(set(versions)), sorted(set(item for item in photos if item))


def wording(check):
    state = check["status"]
    label = {"supported": "符合", "outside": "超出", "missing": "待补", "missing_measure": "缺测量字段"}[state]
    return ("必查" if check["required"] else "参考") + " · " + label


def disposition(checks):
    if any(record["status"] == "outside" and record["required"] for record in checks):
        return "hold"
    if any(record["status"] in ("missing", "missing_measure") and record["required"] for record in checks):
        return "incomplete"
    return "evidence_checked"


def source_art(stored, photo, load_photo, store):
    if stored and stored["artifact_sha"] and stored["payload"].get("format") == "png":
        try:
            data = store.require_blob(stored["artifact_sha"])
            with Image.open(data) as proof:
                return proof.convert("RGBA")
        except OSError:
            raise InputError("所选证据预览图片无法读取") from None
    if photo is not None:
        return load_photo(photo)
    return None


def check_articles(store, checks, selected, read_photo):
    result = []
    for check, evidence in zip(checks, selected):
        photo = store.photo(evidence["photo_id"]) if evidence and evidence["photo_id"] else None
        source = source_art(evidence, photo, read_photo, store)
        visuals = ((source, photo["title"] if photo else "测量结果"),) if source is not None else ()
        symbol = "≤" if check["expectation"] == "maximum" else "≥"
        comparison = f"{check['value']:.4g} / {symbol} {check['threshold']:g}" if check["value"] is not None else "暂无测值"
        lines = (comparison, evidence["title"] if evidence else "尚未指定记录", check["comment"])
        result.append(print_article(visuals, check["name"], lines, flag=wording(check)))
    return result


def closeout(store, firing, raw, read_photo):
    data = obj(raw, "窑次留档检核单")
    name = optional_text(data.get("name"), "检核单名称", 68) or "该窑次留档检核单"
    operator = text(data.get("author"), "填写人", limit=42)
    rules = [Rule.read(raw_rule) for raw_rule in seq(data.get("checks"), "检查项", minimum=1, maximum=14)]
    distinct([rule.id for rule in rules], "检查项")
    checks, selected, versions, photos = resolve(store, firing["id"], rules)
    status = disposition(checks)
    caption = {"hold": "需复检查证", "incomplete": "缺必查证据", "evidence_checked": "所列必查指标均有证据"}[status]
    paper_opts = {"caption": caption, **obj(data.get("paper", {}), "用纸")}
    paper = Paper.read(paper_opts, default_title=name)
    articles = check_articles(store, checks, selected, read_photo)
    result, pages = export_paper(paper, articles, footer=f"釉样台 · {firing['name']} · {operator}")
    return {
        "format": "html", "title": name, "operator": operator, "summary": status, "checks": checks,
        "page_count": pages, "paper": paper.as_json(), "hold_count": sum(item["status"] == "outside" for item in checks),
        "missing_count": sum(item["status"] in ("missing", "missing_measure") for item in checks),
    }, result, photos, versions


def check_csv(row):
    fields = ("id", "name", "status", "required", "expectation", "threshold", "value", "entry_id", "comment")
    flattened = []
    for item in row["checks"]:
        flattened.append({**item, "entry_id": item["entry"]["id"] if item["entry"] else ""})
    return csv_bytes(fields, flattened)
