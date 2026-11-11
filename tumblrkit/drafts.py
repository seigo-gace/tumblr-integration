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
            return [{"id": i, "text": text, "updated": updated,
                     "locked": self._has_publish_receipt(db,i)}
                    for i,text,updated in rows]

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

    @staticmethod
    def _has_publish_receipt(db, ident):
        table=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tumblr_publish_receipts'").fetchone()
        if not table:
            return False
        return db.execute(
            "SELECT 1 FROM tumblr_publish_receipts WHERE draft_id=?",
            (ident,)).fetchone() is not None

    def update(self, ident, text, expected_text):
        """Edit same draft; optimistic CAS and immutable after publish reservation."""
        if (not isinstance(ident,str) or len(ident)!=32
                or any(c not in "0123456789abcdef" for c in ident)
                or not isinstance(text,str) or not text.strip() or len(text)>2000
                or not isinstance(expected_text,str) or len(expected_text)>2000):
            raise ValueError("INVALID_DRAFT")
        value=redact(text.strip())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._has_publish_receipt(db,ident):
                raise ValueError("DRAFT_PUBLISH_LOCKED")
            row=db.execute("SELECT text FROM operation_drafts WHERE id=?",
                           (ident,)).fetchone()
            if row is None:
                raise ValueError("DRAFT_NOT_FOUND")
            if row[0]!=expected_text:
                raise ValueError("DRAFT_EDIT_CONFLICT")
            updated=int(time.time())
            db.execute("UPDATE operation_drafts SET text=?,updated=? WHERE id=?",
                       (value,updated,ident))
        return {"id":ident,"text":value,"updated":updated}

    def delete(self, ident):
        if not isinstance(ident, str) or len(ident) != 32 or not all(c in "0123456789abcdef" for c in ident):
            raise ValueError("INVALID_DRAFT")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._has_publish_receipt(db,ident):
                raise ValueError("DRAFT_PUBLISH_LOCKED")
            result = db.execute("DELETE FROM operation_drafts WHERE id=?", (ident,))
        return result.rowcount > 0
