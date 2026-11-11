"""Local drafts owned only by the Tumblr operation app. No external API."""
import os
import sqlite3
import time
import uuid
from pathlib import Path

from tumblrkit.assistant import redact


class DraftStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        db = sqlite3.connect(str(self.path), timeout=3)
        db.execute("PRAGMA busy_timeout=3000")
        db.execute(
            "CREATE TABLE IF NOT EXISTS operation_drafts("
            "id TEXT PRIMARY KEY, text TEXT NOT NULL, updated INTEGER NOT NULL)"
        )
        db.commit()
        return db

    def save(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("INVALID_DRAFT")
        value = redact(text.strip())
        ident = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT INTO operation_drafts(id,text,updated) VALUES(?,?,?)",
                (ident, value, int(time.time())),
            )
        return {"id": ident, "text": value}

    def list(self):
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,text,updated FROM operation_drafts ORDER BY updated DESC,id DESC LIMIT 100"
            ).fetchall()
        return [{"id": i, "text": text, "updated": updated} for i,text,updated in rows]

    def get(self, ident):
        """Return one exact immutable, app-owned draft by ID for publishing."""
        if (not isinstance(ident,str) or len(ident)!=32
                or any(char not in "0123456789abcdef" for char in ident)):
            raise ValueError("INVALID_DRAFT")
        with self._connect() as db:
            row=db.execute("SELECT id,text,updated FROM operation_drafts WHERE id=?",
                           (ident,)).fetchone()
        if not row:
            raise ValueError("DRAFT_NOT_FOUND")
        return {"id":row[0],"text":row[1],"updated":row[2]}

    def delete(self, ident):
        if not isinstance(ident, str) or len(ident) != 32 or not all(c in "0123456789abcdef" for c in ident):
            raise ValueError("INVALID_DRAFT")
        with self._connect() as db:
            result = db.execute("DELETE FROM operation_drafts WHERE id=?", (ident,))
        return result.rowcount > 0
