"""Material evidence bundles and machine-readable measurement tables."""

import csv
from hashlib import sha256
from io import BytesIO, StringIO
import json
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .inputs import InputError, choice, distinct, flag, in_project, key, obj, optional_text, seq



MAX_PACKET_BYTES = 45 * 1024 * 1024
MEASUREMENT_KINDS = {
    "color", "comparison", "texture", "glare", "pinholes", "spot", "ruler", "line",
    "marks", "grades", "coverage", "palette", "traverse", "cells", "recipe", "crazing",
    "cracks", "phases", "rectify", "relief", "target", "layer",
}
FORMATS = {
    "png": ("image/png", "png"),
    "html": ("text/html; charset=utf-8", "html"),
    "csv": ("text/csv; charset=utf-8", "csv"),
    "zip": ("application/zip", "zip"),
}


def json_bytes(value):
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def safe_cell(text):
    if isinstance(text, str) and text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_bytes(names, rows):
    out = StringIO(newline="")
    records = csv.DictWriter(out, fieldnames=names, extrasaction="ignore")
    records.writeheader()
    for row in rows:
        records.writerow({name: safe_cell(v) for name, v in row.items()})
    return out.getvalue().encode("utf-8-sig")


def packed_csv(data):
    if isinstance(data, (dict, list)):
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return data


def measurement_rows(kind, result):
    collections = {
        "pinholes": ("spots",), "glare": ("regions",), "comparison": ("region_peaks",),
        "marks": ("marks",), "grades": ("samples",), "line": ("points",),
        "texture": ("scales",), "color": ("histogram",), "palette": ("swatches",),
        "traverse": ("segments",), "cells": ("cells",), "recipe": ("cells",),
        "crazing": ("junctions",), "cracks": ("partitions",), "phases": ("phases",), "rectify": ("scale_grid",),
        "relief": ("regions",), "target": ("patches",), "layer": ("stations",),
    }
    for field in collections.get(kind, ()):
        selected = result.get(field, [])
        if selected:
            all_fields = list(dict.fromkeys(name for row in selected for name in row))
            return all_fields, [{name: packed_csv(value) for name, value in row.items()} for row in selected]
    return ["measure", "value"], [{"measure": name, "value": packed_csv(value)} for name, value in result.items()]


def csv_report(record):
    if record["kind"] not in MEASUREMENT_KINDS:
        raise InputError("该记录没有可下载的测量表")
    result = record["payload"].get("result", {})
    cols, rows = measurement_rows(record["kind"], result)
    return csv_bytes(cols, rows)


def record_for_packet(entry):
    return {
        "id": entry["id"], "source_photo": entry["photo_id"], "kind": entry["kind"],
        "title": entry["title"], "revision": entry["revision"], "changed_at": entry["updated"],
        "payload": entry["payload"], "artifact_hash": entry["artifact_sha"],
    }


def photo_for_packet(entry):
    fields = ("id", "title", "role", "note", "width", "height", "source_sha", "png_sha", "original_format")
    return {field: entry[field] for field in fields}


def add_zip_file(archive, name, data):
    item = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    item.external_attr = 0o100644 << 16
    item.compress_type = ZIP_DEFLATED
    archive.writestr(item, data)


def make_packet(store, firing, user):
    data = obj(user, "封存包")
    title = optional_text(data.get("title"), "封存名目", 100) or f"{firing['name']} · 图像物证封存"
    ids = [key(v, "封存记录") for v in seq(data.get("entries"), "封存记录", minimum=1, maximum=28)]
    photos_requested = [key(v, "封存照片") for v in seq(data.get("photos", []), "封存照片", maximum=24)]
    distinct(ids, "封存记录")
    distinct(photos_requested, "封存照片")
    include_assets = flag(data.get("include_assets", True), "带入图片原片")
    mode = choice(data.get("measurement", "csv"), "测量表", ("none", "csv"))

    with store.connect() as db:
        db.execute("BEGIN")
        entries = [in_project(store.entry(i, db), firing["id"], "封存记录") for i in ids]
        if any(entry["kind"] == "packet" for entry in entries):
            raise InputError("选择封存包内的原始记录")
        sources = set(photos_requested)
        for record in entries:
            if record["photo_id"]:
                sources.add(record["photo_id"])
            source_rows = db.execute("SELECT photo_id FROM entry_photos WHERE entry_id=?", (record["id"],))
            sources.update(row["photo_id"] for row in source_rows)
        if len(sources) > 24:
            raise InputError("一个物证封存包最多包括 24 张来源照片")
        photos = [in_project(store.photo(i, db), firing["id"], "封存照片") for i in sorted(sources)]

    facts = {
        "schema": "kilnledger-packet-v1", "title": title,
        "firing": {field: firing[field] for field in ("name", "id", "kiln", "fired_on", "notes", "revision")},
        "includes_photos": include_assets, "photo_count": len(photos),
        "entries": [record_for_packet(record) for record in entries],
        "photos": [photo_for_packet(photo) for photo in photos],
    }
    binaries = {}
    for record in entries:
        stem = f"records/{record['id']}"
        binaries[f"{stem}.json"] = json_bytes(record_for_packet(record))
        if mode == "csv" and record["kind"] in MEASUREMENT_KINDS:
            binaries[f"{stem}.csv"] = csv_report(record)
        if record["artifact_sha"]:
            _, extension = FORMATS[record["payload"]["format"]]
            binaries[f"{stem}.{extension}"] = store.require_blob(record["artifact_sha"])
    if include_assets:
        for photo in photos:
            binaries[f"photos/{photo['id']}.png"] = store.require_blob(photo["png_sha"])

    def size(value):
        return len(value) if isinstance(value, bytes) else value.stat().st_size

    if sum(size(value) for value in binaries.values()) > MAX_PACKET_BYTES:
        raise InputError("封存内容超过 45 MiB，可取消原片或选较小的记录集", status=413)
    manifest = []
    stream = BytesIO()
    with ZipFile(stream, "w", allowZip64=False) as archive:
        for path in sorted(binaries):
            content = binaries[path]
            if not isinstance(content, bytes):
                content = content.read_bytes()
            manifest.append({"path": path, "sha256": sha256(content).hexdigest(), "size": len(content)})
            add_zip_file(archive, path, content)
        facts["files"] = manifest
        add_zip_file(archive, "manifest.json", json_bytes(facts))
    payload = {
        "title": title, "photo_count": facts["photo_count"], "entry_count": len(entries),
        "files": manifest, "file_count": len(manifest), "origin_ids": ids,
        "includes_photos": include_assets, "firing_revision": facts["firing"]["revision"],
    }
    source_revisions = [(entry["id"], entry["revision"]) for entry in entries]
    return payload, stream.getvalue(), sorted(sources), source_revisions


def packet_index(entry):
    data = record_for_packet(entry)
    return json_bytes({
        "project": "KilnLedger", "format": "kilnledger-packet-list-v1", "packet": data,
        "archive_sha": entry["artifact_sha"],
    })
