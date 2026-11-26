"""Local drafts owned only by the Tumblr operation app. No external API."""
import os
import hashlib
import json
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
        columns=[row[1] for row in db.execute("PRAGMA table_info(operation_drafts)")]
        if "meta" not in columns:
            db.execute(
                "ALTER TABLE operation_drafts ADD COLUMN meta TEXT NOT NULL DEFAULT ''")
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

    def list(self,offset=0,limit=100):
        # Page size is a response budget; all persisted drafts remain reachable.
        if (type(offset) is not int or not 0<=offset<=9223372036854775807
                or type(limit) is not int or not 1<=limit<=100):
            raise ValueError("INVALID_DRAFT_PAGE")
        with self._connect() as db:
            rows=db.execute(
                "SELECT id,text,updated FROM operation_drafts "
                "ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?",(limit,offset)
            ).fetchall()
            result=[]
            for ident,text,updated in rows:
                status,post_id,current=self._published_info(db,ident,text)
                result.append({
                    "id":ident,"text":text,"updated":updated,
                    "text_digest":hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "locked":self._has_publish_receipt(db,ident),
                    "publish_state":status,"provider_post_id":post_id,
                    "current_text":current,
                })
            return result

    def count(self):
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM operation_drafts").fetchone()[0]

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

    def preview(self, ident):
        """Exact stored text and digest for a single owner approval preview.

        Never mutates or publishes; caller must separately confirm the
        corresponding original Bridge publishing action.
        """
        self.get(ident)  # Canonical ID validation and absence handling.
        with self._connect() as db:
            row=db.execute("SELECT text,updated FROM operation_drafts WHERE id=?",
                           (ident,)).fetchone()
            if row is None:
                raise ValueError("DRAFT_NOT_FOUND")
            text,updated=row
            state,post_id,current=self._published_info(db,ident,text)
            return {"verified":True,"draft_id":ident,"text":text,
                    "text_digest":hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "updated":updated,"publish_state":state,
                    "locked":self._has_publish_receipt(db,ident),
                    "provider_post_id":post_id,
                    "external_publication":False}

    @staticmethod
    def _has_publish_receipt(db, ident):
        table=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tumblr_publish_receipts'").fetchone()
        if table and db.execute(
            "SELECT 1 FROM tumblr_publish_receipts WHERE draft_id=?",
            (ident,)).fetchone() is not None:
            return True
        # A scheduled post must retain its exact saved body through the
        # due-time provider result. Cancelling before send unlocks it again.
        scheduled=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='scheduled_publications'").fetchone()
        if scheduled and db.execute(
            "SELECT 1 FROM scheduled_publications WHERE draft_id=? "
            "AND state IN ('pending','sending','needs_reconcile') LIMIT 1",
            (ident,)).fetchone() is not None:
            return True
        has_mutations=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tumblr_post_mutations'").fetchone()
        return bool(has_mutations and db.execute(
            "SELECT 1 FROM tumblr_post_mutations WHERE revision_draft_id=?",
            (ident,)).fetchone())

    @staticmethod
    def _published_info(db,ident,text):
        has_receipt=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tumblr_publish_receipts'").fetchone()
        if not has_receipt:
            return "local",None,text
        row=db.execute(
            "SELECT post_id,state FROM tumblr_publish_receipts WHERE draft_id=?",
            (ident,)).fetchone()
        if not row:
            return "local",None,text
        post_id,state=row
        has_mutations=db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='tumblr_post_mutations'").fetchone()
        current=text
        if has_mutations:
            operations=db.execute(
                "SELECT action,state,revision_draft_id FROM tumblr_post_mutations "
                "WHERE source_draft_id=? ORDER BY rowid ASC",(ident,)).fetchall()
            for action,change_state,revision in operations:
                if action=="edit" and change_state=="verified":
                    changed=db.execute(
                        "SELECT text FROM operation_drafts WHERE id=?",(revision,)).fetchone()
                    if changed:
                        current=changed[0]
                if change_state in ("reserved","sent"):
                    state="changing"
                if action=="delete" and change_state=="verified":
                    state="deleted"
        return ("published" if state=="verified" else state),post_id,current

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

    def _validated_id(self, ident):
        if (not isinstance(ident,str) or len(ident)!=32
                or any(c not in "0123456789abcdef" for c in ident)):
            raise ValueError("INVALID_DRAFT")
        return ident

    def meta_of(self, ident):
        """Owner-private annotation. Empty when the draft has no attachment/tag/link."""
        self._validated_id(ident)
        with self._connect() as db:
            row=db.execute(
                "SELECT meta FROM operation_drafts WHERE id=?",(ident,)).fetchone()
        if row is None:
            raise ValueError("DRAFT_NOT_FOUND")
        if not row[0]:
            return {}
        try:
            value=json.loads(row[0])
        except ValueError:
            return {}
        return value if isinstance(value,dict) else {}

    def set_meta(self, ident, expected_text, meta):
        """Attach owner metadata without changing the saved text or publishing."""
        self._validated_id(ident)
        if (not isinstance(expected_text,str) or len(expected_text)>2000
                or not isinstance(meta,dict)):
            raise ValueError("INVALID_DRAFT")
        raw=json.dumps(meta,ensure_ascii=False,separators=(",",":"),sort_keys=True)
        if len(raw)>4000:
            raise ValueError("INVALID_DRAFT")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._has_publish_receipt(db,ident):
                raise ValueError("DRAFT_PUBLISH_LOCKED")
            row=db.execute(
                "SELECT text FROM operation_drafts WHERE id=?",(ident,)).fetchone()
            if row is None:
                raise ValueError("DRAFT_NOT_FOUND")
            if row[0]!=expected_text:
                raise ValueError("DRAFT_EDIT_CONFLICT")
            db.execute(
                "UPDATE operation_drafts SET meta=?,updated=? WHERE id=?",
                (raw,int(time.time()),ident))
        return {"id":ident,"text":expected_text,"meta":meta,"published":False}

    def reuse(self, ident):
        """Copy one saved draft into a new unlocked draft. Does not publish."""
        current=self.get(ident)
        saved=self.save(current["text"])
        meta={"catalog":"publisher.03","source_draft_id":ident}
        self.set_meta(saved["id"],saved["text"],meta)
        return {"id":saved["id"],"text":saved["text"],
                "source_draft_id":ident,"published":False}

    def delete(self, ident):
        if not isinstance(ident, str) or len(ident) != 32 or not all(c in "0123456789abcdef" for c in ident):
            raise ValueError("INVALID_DRAFT")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._has_publish_receipt(db,ident):
                raise ValueError("DRAFT_PUBLISH_LOCKED")
            result = db.execute("DELETE FROM operation_drafts WHERE id=?", (ident,))
        return result.rowcount > 0
