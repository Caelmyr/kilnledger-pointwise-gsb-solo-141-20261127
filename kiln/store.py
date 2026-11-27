"""Persistent firing folders, immutable picture files and revisioned records."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import uuid4

from .inputs import Conflict, InputError, NotFound


SCHEMA = """
CREATE TABLE IF NOT EXISTS firings (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kiln TEXT NOT NULL,
    fired_on TEXT NOT NULL,
    notes TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0)
);
CREATE TABLE IF NOT EXISTS photos (
    id TEXT PRIMARY KEY,
    firing_id TEXT NOT NULL REFERENCES firings(id),
    title TEXT NOT NULL,
    attachment_name TEXT NOT NULL,
    original_format TEXT NOT NULL,
    source_sha TEXT NOT NULL,
    png_sha TEXT NOT NULL,
    preview_sha TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    had_profile INTEGER NOT NULL,
    exif_oriented INTEGER NOT NULL,
    note TEXT NOT NULL,
    role TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    UNIQUE(firing_id, title)
);
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY,
    firing_id TEXT NOT NULL REFERENCES firings(id),
    photo_id TEXT REFERENCES photos(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    artifact_sha TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    unique_key TEXT,
    UNIQUE(firing_id, unique_key)
);
CREATE TABLE IF NOT EXISTS entry_photos (
    entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    photo_id TEXT NOT NULL REFERENCES photos(id),
    PRIMARY KEY (entry_id, photo_id)
);
CREATE INDEX IF NOT EXISTS photos_by_firing ON photos(firing_id, created);
CREATE INDEX IF NOT EXISTS entries_by_firing ON entries(firing_id, kind, updated);
CREATE INDEX IF NOT EXISTS photos_by_entry ON entry_photos(photo_id, entry_id);
"""


class Store:
    def __init__(self, path):
        self.root = Path(path)
        self.root.mkdir(parents=True, exist_ok=True)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(exist_ok=True)
        self.database = self.root / "kiln.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self, writable=False):
        database = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        try:
            if writable:
                database.execute("BEGIN IMMEDIATE")
            yield database
            if writable:
                database.commit()
        except Exception:
            if writable:
                database.rollback()
            raise
        finally:
            database.close()

    @staticmethod
    def identifier(prefix):
        return f"{prefix}-{uuid4().hex[:16]}"

    @staticmethod
    def stamp():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def blob_path(self, digest):
        if len(digest) != 64 or set(digest).difference("abcdef0123456789"):
            raise ValueError("unexpected blob digest")
        return self.blobs / digest[:2] / digest[2:]

    def require_blob(self, digest):
        path = self.blob_path(digest)
        if not path.is_file():
            raise NotFound("归档文件已缺失")
        return path

    def put_blob(self, content):
        digest = sha256(content).hexdigest()
        target = self.blob_path(digest)
        if target.exists():
            return digest
        target.parent.mkdir(exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile(dir=target.parent, prefix=".upload-", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return digest

    @staticmethod
    def must_find(db, sql, args, description):
        record = db.execute(sql, args).fetchone()
        if record is None:
            raise NotFound(description)
        return dict(record)

    @staticmethod
    def require_revision(record, expected):
        if record["revision"] != expected:
            raise Conflict("内容已被更新，请刷新后重新操作")

    def list_firings(self):
        with self.connect() as db:
            records = db.execute(
                """SELECT f.*, (SELECT count(*) FROM photos p WHERE p.firing_id=f.id) AS photo_count,
                   (SELECT count(*) FROM entries e WHERE e.firing_id=f.id) AS entry_count
                   FROM firings f ORDER BY f.updated DESC, f.id"""
            )
            return [dict(record) for record in records]

    def firing(self, firing_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.firing(firing_id, connection)
        return self.must_find(db, "SELECT * FROM firings WHERE id=?", (firing_id,), "找不到该窑次")

    def create_firing(self, values):
        firing_id = self.identifier("f")
        now = self.stamp()
        with self.connect(True) as db:
            db.execute(
                "INSERT INTO firings VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (firing_id, values["name"], values["kiln"], values["fired_on"], values["notes"], now, now),
            )
            return self.firing(firing_id, db)

    def update_firing(self, firing_id, values, revision):
        with self.connect(True) as db:
            self.require_revision(self.firing(firing_id, db), revision)
            db.execute(
                """UPDATE firings SET name=?, kiln=?, fired_on=?, notes=?, updated=?,
                   revision=revision+1 WHERE id=?""",
                (values["name"], values["kiln"], values["fired_on"], values["notes"], self.stamp(), firing_id),
            )
            return self.firing(firing_id, db)

    def photo(self, photo_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.photo(photo_id, connection)
        row = self.must_find(db, "SELECT * FROM photos WHERE id=?", (photo_id,), "找不到该照片")
        for name in ("had_profile", "exif_oriented"):
            row[name] = bool(row[name])
        return row

    def photos(self, firing_id, *, role=None, search=None):
        filters = ["firing_id=?"]
        args = [firing_id]
        if role:
            filters.append("role=?")
            args.append(role)
        if search:
            filters.append("(instr(lower(title), lower(?)) OR instr(lower(note), lower(?)))")
            args.extend((search, search))
        where = " AND ".join(filters)
        with self.connect() as db:
            self.firing(firing_id, db)
            rows = db.execute(f"SELECT id FROM photos WHERE {where} ORDER BY created, id", args).fetchall()
            return [self.photo(row["id"], db) for row in rows]

    def create_photo(self, firing_id, record):
        photo_id = self.identifier("p")
        fields = (
            "title", "attachment_name", "original_format", "source_sha", "png_sha", "preview_sha",
            "width", "height", "had_profile", "exif_oriented", "note", "role",
        )
        with self.connect(True) as db:
            self.firing(firing_id, db)
            count = db.execute("SELECT count(*) FROM photos WHERE firing_id=?", (firing_id,)).fetchone()[0]
            if count >= 80:
                raise Conflict("单窑次可归档最多 80 张照片")
            stamp = self.stamp()
            try:
                db.execute(
                    """INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                    (photo_id, firing_id, *(record[field] for field in fields), stamp, stamp),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该窑次已有同名照片") from None
            db.execute("UPDATE firings SET updated=? WHERE id=?", (stamp, firing_id))
            return self.photo(photo_id, db)

    def update_photo(self, photo_id, data, revision):
        with self.connect(True) as db:
            self.require_revision(self.photo(photo_id, db), revision)
            try:
                db.execute(
                    "UPDATE photos SET title=?, note=?, role=?, updated=?, revision=revision+1 WHERE id=?",
                    (data["title"], data["note"], data["role"], self.stamp(), photo_id),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该窑次已有同名照片") from None
            return self.photo(photo_id, db)

    def delete_photo(self, photo_id, revision):
        with self.connect(True) as db:
            row = self.photo(photo_id, db)
            self.require_revision(row, revision)
            result = db.execute(
                """SELECT e.title FROM entries e LEFT JOIN entry_photos ep ON ep.entry_id=e.id
                   WHERE e.photo_id=? OR ep.photo_id=? LIMIT 1""",
                (photo_id, photo_id),
            ).fetchone()
            if result is not None:
                raise Conflict(f"照片仍被「{result['title']}」使用")
            db.execute("DELETE FROM photos WHERE id=?", (photo_id,))

    @staticmethod
    def entry_data(row):
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def entry(self, entry_id, db=None):
        if db is None:
            with self.connect() as connection:
                return self.entry(entry_id, connection)
        return self.entry_data(self.must_find(db, "SELECT * FROM entries WHERE id=?", (entry_id,), "找不到该记录"))

    def entries(self, firing_id, *, kind=None, photo_id=None):
        filters = ["firing_id=?"]
        args = [firing_id]
        if kind:
            filters.append("kind=?")
            args.append(kind)
        if photo_id:
            filters.append("photo_id=?")
            args.append(photo_id)
        where = " AND ".join(filters)
        with self.connect() as db:
            self.firing(firing_id, db)
            rows = db.execute(f"SELECT * FROM entries WHERE {where} ORDER BY updated DESC, id", args)
            return [self.entry_data(row) for row in rows]

    def latest(self, firing_id, photo_id, kind):
        with self.connect() as db:
            row = db.execute(
                """SELECT * FROM entries WHERE firing_id=? AND photo_id=? AND kind=?
                   ORDER BY updated DESC, id LIMIT 1""",
                (firing_id, photo_id, kind),
            ).fetchone()
            return self.entry_data(row) if row is not None else None

    def validate_sources(self, db, firing_id, photo_id, references):
        self.firing(firing_id, db)
        all_sources = sorted(set(references) | ({photo_id} if photo_id else set()))
        for source in all_sources:
            stored = self.photo(source, db)
            if stored["firing_id"] != firing_id:
                raise InputError("记录内的来源照片需要属于同一窑次")
        return all_sources

    @staticmethod
    def serialize(data):
        return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))

    def create_entry(self, firing_id, kind, title, payload, *, photo_id=None, references=(),
                     artifact=None, unique_key=None, expected_sources=()):
        stamp = self.stamp()
        entry_id = self.identifier("e")
        with self.connect(True) as db:
            sources = self.validate_sources(db, firing_id, photo_id, references)
            for source_id, source_revision in expected_sources:
                self.require_revision(self.entry(source_id, db), source_revision)
            count = db.execute("SELECT count(*) FROM entries WHERE firing_id=?", (firing_id,)).fetchone()[0]
            if count >= 300:
                raise Conflict("该窑次已达到 300 条保留记录")
            try:
                db.execute(
                    """INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                    (entry_id, firing_id, photo_id, kind, title, self.serialize(payload),
                     artifact, stamp, stamp, unique_key),
                )
            except sqlite3.IntegrityError:
                raise Conflict("已有同一身份的记录，请刷新后操作") from None
            db.executemany("INSERT INTO entry_photos VALUES (?, ?)", [(entry_id, source) for source in sources])
            db.execute("UPDATE firings SET updated=? WHERE id=?", (stamp, firing_id))
            return self.entry(entry_id, db)

    def replace_entry(self, entry_id, revision, title, payload, *, artifact=None, references=()):
        with self.connect(True) as db:
            existing = self.entry(entry_id, db)
            self.require_revision(existing, revision)
            sources = self.validate_sources(db, existing["firing_id"], existing["photo_id"], references)
            db.execute(
                """UPDATE entries SET title=?, payload=?, artifact_sha=?, updated=?,
                   revision=revision+1 WHERE id=?""",
                (title, self.serialize(payload), artifact, self.stamp(), entry_id),
            )
            db.execute("DELETE FROM entry_photos WHERE entry_id=?", (entry_id,))
            db.executemany("INSERT INTO entry_photos VALUES (?, ?)", [(entry_id, source) for source in sources])
            return self.entry(entry_id, db)

    def delete_entry(self, entry_id, revision):
        with self.connect(True) as db:
            existing = self.entry(entry_id, db)
            self.require_revision(existing, revision)
            if existing["kind"] == "packet":
                raise Conflict("封存包需作为物证保留")
            db.execute("DELETE FROM entries WHERE id=?", (entry_id,))
