"""Private, app-owned chat attachments. No Tumblr or AI-provider network calls."""
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
MAX_NAME_CHARS = 120
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
OFFICE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}
ALLOWED_TYPES = IMAGE_TYPES | {"application/pdf", "text/plain"} | set(OFFICE_TYPES.values())
ID_RE = re.compile(r"[0-9a-f]{32}\Z")


class AttachmentError(ValueError):
    pass


def sniff_type(data, name=""):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith((bytes.fromhex("504b0304"), bytes.fromhex("504b0506"))):
        extension = Path(name).suffix.lower()
        if extension in OFFICE_TYPES:
            return OFFICE_TYPES[extension]
        raise AttachmentError("UNSUPPORTED_FILE_TYPE")
    try:
        decoded = data.decode("utf-8")
        if "\x00" not in decoded:
            return "text/plain"
    except UnicodeError:
        pass
    raise AttachmentError("UNSUPPORTED_FILE_TYPE")


class AttachmentStore:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "attachments"
        self.db_path = Path(data_dir) / "attachments.sqlite3"

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.db_path.exists():
            fd = os.open(self.db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.execute("PRAGMA busy_timeout=5000")
        db.execute(
            "CREATE TABLE IF NOT EXISTS chat_attachments("
            "id TEXT PRIMARY KEY, name TEXT NOT NULL, media_type TEXT NOT NULL,"
            "size INTEGER NOT NULL, created INTEGER NOT NULL)"
        )
        db.commit()
        return db

    def save(self, name, data):
        if not isinstance(data, bytes) or not data or len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("INVALID_FILE_SIZE")
        if not isinstance(name, str):
            raise AttachmentError("INVALID_FILENAME")
        name = name.replace("\\", "/").split("/")[-1].strip()
        if not name or len(name) > MAX_NAME_CHARS or any(ord(c) < 32 for c in name):
            raise AttachmentError("INVALID_FILENAME")
        media_type = sniff_type(data, name)
        if media_type not in ALLOWED_TYPES:
            raise AttachmentError("UNSUPPORTED_FILE_TYPE")
        ident = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        filepath = self.root / ident
        fd = os.open(filepath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            with self._connect() as db:
                db.execute(
                    "INSERT INTO chat_attachments(id,name,media_type,size,created)"
                    " VALUES(?,?,?,?,?)",
                    (ident, name, media_type, len(data), int(time.time())),
                )
        except Exception:
            filepath.unlink(missing_ok=True)
            raise
        return {"id": ident, "name": name, "media_type": media_type,
                "size": len(data)}

    def get(self, ident):
        if not isinstance(ident, str) or ID_RE.fullmatch(ident) is None:
            raise AttachmentError("INVALID_ATTACHMENT_ID")
        with self._connect() as db:
            row = db.execute(
                "SELECT name,media_type,size FROM chat_attachments WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            return None
        path = self.root / ident
        if not path.is_file() or path.is_symlink():
            return None
        name, media_type, size = row
        if path.stat().st_size != size:
            return None
        return {"id": ident, "name": name, "media_type": media_type,
                "size": size, "data": path.read_bytes()}

    def delete(self, ident):
        if not isinstance(ident, str) or ID_RE.fullmatch(ident) is None:
            raise AttachmentError("INVALID_ATTACHMENT_ID")
        with self._connect() as db:
            deleted = db.execute("DELETE FROM chat_attachments WHERE id=?", (ident,)).rowcount
        if deleted:
            (self.root / ident).unlink(missing_ok=True)
        return bool(deleted)

    def describe(self, ids):
        if not isinstance(ids, list) or len(ids) > 5 or len(set(ids)) != len(ids):
            raise AttachmentError("INVALID_ATTACHMENT_IDS")
        records = []
        for ident in ids:
            record = self.get(ident)
            if record is None:
                raise AttachmentError("UNKNOWN_ATTACHMENT")
            records.append({key: record[key] for key in ("id", "name", "media_type", "size")})
        return records
