"""Verify a portable archive's files, source photos and referenced measurements."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import json
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from PIL import Image, UnidentifiedImageError

from .archive import FORMATS
from .inputs import InputError, seq, text


MAX_ARCHIVE = 49_000_000
MAX_UNPACKED = 50_000_000
MAX_MANIFEST = 2_000_000
MAX_ITEMS = 205
DEFLATE = (ZIP_STORED, ZIP_DEFLATED)


def signature(value, field):
    if not isinstance(value, str) or len(value) != 64 or set(value).difference("0123456789abcdef"):
        raise InputError(f"{field}缺少有效 SHA-256")
    return value


def archive_path(value):
    if not isinstance(value, str):
        raise InputError("归档文件名需要是字符串")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value or value.startswith(".") or
            any(part in ("..", ".") for part in value.split("/")) or str(path) != value):
        raise InputError("归档含有无法核验的文件路径")
    if len(value) > 100 or not value:
        raise InputError("归档文件路径长度无效")
    return value


def parse_object(data, field):
    try:
        result = json.loads(data)
    except (UnicodeError, ValueError, RecursionError):
        raise InputError(f"{field}的 JSON 无法读取") from None
    if not isinstance(result, dict):
        raise InputError(f"{field}应当为一个 JSON 对象")
    return result


def inspect_zip(zipped):
    registered = {}
    total = 0
    items = zipped.infolist()
    if len(items) > MAX_ITEMS:
        raise InputError("单个归档超过文件数量上限")
    for item in items:
        name = archive_path(item.filename)
        if item.is_dir() or item.compress_type not in DEFLATE or item.flag_bits & 1:
            raise InputError("归档包含目录、无法核验的压缩方式或加密数据")
        if name in registered:
            raise InputError("归档内出现同名文件")
        total += item.file_size
        if total > MAX_UNPACKED:
            raise InputError("展开后的归档文件超过验证限额", status=413)
        registered[name] = item
    item = registered.get("manifest.json")
    if item is None or item.file_size > MAX_MANIFEST:
        raise InputError("归档缺少可读取的 manifest.json")
    return registered


@dataclass(frozen=True)
class Listed:
    path: str
    size: int
    checksum: str

    @classmethod
    def read(cls, raw):
        if not isinstance(raw, dict):
            raise InputError("封存文件清单存在无效项")
        path = archive_path(raw.get("path"))
        size = raw.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_UNPACKED:
            raise InputError("有封存文件没有合理的字节数")
        check = signature(raw.get("sha256"), "文件清单")
        return cls(path, size, check)


class Inspection:
    def __init__(self, zipper, items):
        self.zipper = zipper
        self.items = items
        self.content = {}
        self.digests = {}
        self.issues = []
        self.checked_records = []
        self.checked_photos = []

    def report(self, kind, path, message):
        self.issues.append({"type": kind, "path": path, "detail": message})

    def index_contents(self, manifest):
        listed = {}
        for record in seq(manifest.get("files"), "封存文件清单", maximum=MAX_ITEMS):
            item = Listed.read(record)
            if item.path == "manifest.json" or item.path in listed:
                raise InputError("封存清单路径重复或包括清单本身")
            listed[item.path] = item
        for name in sorted(set(self.items) - set(listed) - {"manifest.json"}):
            self.report("extra", name, "文件没有列在清单")
        for name, item in listed.items():
            if name not in self.items:
                self.report("missing", name, "清单列出的文件实际缺失")
                continue
            info = self.items[name]
            if info.file_size != item.size:
                self.report("size", name, "文件字节数和清单不一致")
            try:
                data = self.zipper.read(info)
            except (BadZipFile, OSError, RuntimeError):
                self.report("unreadable", name, "文件读出错误")
                continue
            digest = sha256(data).hexdigest()
            if digest != item.checksum:
                self.report("changed", name, "内容摘要和清单不同")
            self.content[name] = data
            self.digests[name] = digest
        return len(listed)

    def known_photos(self, items, includes_photos):
        photos = set()
        for record in seq(items, "来源照片", maximum=24):
            if not isinstance(record, dict):
                raise InputError("来源照片清单存在无效项")
            photo_id = text(record.get("id"), "照片标识", limit=35)
            if photo_id in photos:
                raise InputError("来源照片重复登记")
            photos.add(photo_id)
            path = archive_path(f"photos/{photo_id}.png")
            expected_hash = signature(record.get("png_sha"), "照片")
            if includes_photos:
                self.photo_content(expected_hash, path, record)
            self.checked_photos.append({"id": photo_id, "path": path if includes_photos else None,
                                        "verified_contents": path in self.content})
        return photos

    def photo_content(self, signature_value, path, record):
        if path not in self.content:
            self.report("photo_missing", path, "清单说包含原片，而图片缺失")
            return
        if self.digests[path] != signature_value:
            self.report("photo_digest", path, "图片与记录里的归档照片摘要不同")
        try:
            with Image.open(BytesIO(self.content[path])) as image:
                expected = (record.get("width"), record.get("height"))
                if image.format != "PNG" or image.size != expected:
                    self.report("photo_dimensions", path, "尺寸或格式与原片登记不同")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError, ValueError):
            self.report("photo_decode", path, "不能读出归档里的 PNG")

    def validate_records(self, items, photos):
        entries = set()
        for record in seq(items, "封存记录清单", minimum=1, maximum=28):
            if not isinstance(record, dict):
                raise InputError("封存记录清单存在无效项")
            entry_id = text(record.get("id"), "封存记录编号", limit=35)
            if entry_id in entries:
                raise InputError("同一条封存记录不能出现两次")
            entries.add(entry_id)
            path = archive_path(f"records/{entry_id}.json")
            known = self.entries_contents(record, path)
            first = record.get("source_photo")
            if first is not None and (not isinstance(first, str) or first not in photos):
                self.report("unknown_photo", path, "记录的主照片未登记在来源清单")
            self.check_secondary_sources(known.get("payload", {}), path, photos)
            artifact_hash = record.get("artifact_hash")
            payload = record.get("payload")
            if artifact_hash and isinstance(payload, dict):
                fmt = payload.get("format")
                values = FORMATS.get(fmt) if isinstance(fmt, str) else None
                if values is None:
                    self.report("artifact_format", path, "记录的成果格式无法识别")
                else:
                    artifact = f"records/{entry_id}.{values[1]}"
                    self.artifact_contents(artifact, artifact_hash)
            self.checked_records.append({"id": entry_id, "path": path, "verified_contents": path in self.content})
        return entries

    def entries_contents(self, record, path):
        if path not in self.content:
            self.report("record_missing", path, "清单须同时带入对应记录 JSON")
            return {}
        try:
            parsed = parse_object(self.content[path], "封存记录")
        except InputError:
            self.report("record_json", path, "无法读取记录 JSON")
            return {}
        if parsed != record:
            self.report("record_mismatch", path, "记录 JSON 与封存索引的原始信息不同")
        return parsed

    def artifact_contents(self, path, digest):
        if path not in self.digests:
            self.report("artifact_missing", path, "记录所引用的成果文件缺失")
        elif self.digests[path] != digest:
            self.report("artifact_digest", path, "成果文件与记录所记摘要不同")

    def check_secondary_sources(self, payload, path, photos):
        if not isinstance(payload, dict):
            self.report("record_payload", path, "归档记录内容不是一份对象")
            return
        result = payload.get("result", {})
        if not isinstance(result, dict):
            result = {}
        related = []
        for collection in ("cards", "items", "source_samples"):
            rows = payload.get(collection, result.get(collection, []))
            if isinstance(rows, list):
                related.extend(item["photo_id"] for item in rows if isinstance(item, dict) and
                               isinstance(item.get("photo_id"), str))
        for field in ("comparison_id", "reference_id"):
            other = result.get(field)
            if isinstance(other, str):
                related.append(other)
        for source in sorted(set(related) - photos):
            self.report("unknown_photo", path, f"记录引用的照片 {source} 未登记")


def verify(stream):
    data = stream.read(MAX_ARCHIVE + 1)
    if not data:
        raise InputError("请选择一份本地 ZIP 封存包")
    if len(data) > MAX_ARCHIVE:
        raise InputError("封存包超过可核验大小", status=413)
    try:
        with ZipFile(BytesIO(data), "r") as zipped:
            contents = inspect_zip(zipped)
            manifest = parse_object(zipped.read(contents["manifest.json"]), "封存清单")
            if manifest.get("schema") != "kilnledger-packet-v1":
                raise InputError("该 ZIP 没有可识别的釉样台封存清单")
            audit = Inspection(zipped, contents)
            count = audit.index_contents(manifest)
            includes = manifest.get("includes_photos")
            if not isinstance(includes, bool):
                raise InputError("封存清单没有明确是否带入原片")
            photos = audit.known_photos(manifest.get("photos"), includes)
            entries = audit.validate_records(manifest.get("entries"), photos)
            if len(photos) != manifest.get("photo_count"):
                audit.report("photo_count", "manifest.json", "照片数量与来源清单不同")
    except (BadZipFile, OSError, RuntimeError, EOFError):
        raise InputError("不能读取该封存 ZIP") from None

    return {
        "valid": not audit.issues,
        "title": manifest.get("title"), "entries": len(entries), "photos": len(photos),
        "lists": count, "file_sha256": sha256(data).hexdigest(), "included_photos": includes,
        "issues": audit.issues, "checked_records": audit.checked_records, "checked_photos": audit.checked_photos,
    }
