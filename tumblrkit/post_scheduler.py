"""Durable owner-confirmed scheduled Tumblr drafts.

Stores only schedule metadata in the existing private DraftStore SQLite.
Uses the existing PublisherAdapter for one actual send and provider readback.
An uncertain send is never auto-retried after process restart.
"""
import hashlib
import sqlite3
import time
import uuid
from contextlib import closing


SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_publications(
 schedule_id TEXT PRIMARY KEY,
 draft_id TEXT NOT NULL REFERENCES operation_drafts(id),
 expected_digest TEXT NOT NULL,
 publish_at INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN
 ('pending','sending','verified','needs_reconcile','cancelled')),
 post_id TEXT,
 updated_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS scheduled_active_draft
 ON scheduled_publications(draft_id)
 WHERE state IN ('pending','sending','needs_reconcile');
"""


class ScheduledPublisher:
    def __init__(self, drafts, publisher=None, clock=None):
        self.drafts=drafts
        self.publisher=publisher
        self.clock=clock or time.time
        with closing(self.drafts._connect()) as db:
            db.executescript(SCHEMA)

    @staticmethod
    def _digest(text):
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def queue(self, draft_id, publish_at, expected_digest):
        if type(publish_at) is not int or publish_at <= int(self.clock()):
            raise ValueError("schedule_time_invalid")
        if (not isinstance(expected_digest,str) or
                len(expected_digest)!=64 or
                any(ch not in "0123456789abcdef" for ch in expected_digest)):
            raise ValueError("schedule_digest_invalid")
        schedule_id=uuid.uuid4().hex
        now=int(self.clock())
        with closing(self.drafts._connect()) as db:
            db.execute(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            try:
                row=db.execute("SELECT text FROM operation_drafts WHERE id=?",
                               (draft_id,)).fetchone()
                if not row or self._digest(row[0])!=expected_digest:
                    raise ValueError("schedule_draft_changed")
                existing=db.execute(
                    "SELECT schedule_id,publish_at,expected_digest FROM scheduled_publications "
                    "WHERE draft_id=? AND state IN ('pending','sending','needs_reconcile')",
                    (draft_id,)).fetchone()
                if existing:
                    if existing[1]==publish_at and existing[2]==expected_digest:
                        db.commit()
                        return {"schedule_id":existing[0],"state":"already_scheduled"}
                    raise ValueError("schedule_existing_operation")
                db.execute(
                    "INSERT INTO scheduled_publications "
                    "(schedule_id,draft_id,expected_digest,publish_at,state,updated_at) "
                    "VALUES(?,?,?,?,'pending',?)",
                    (schedule_id,draft_id,expected_digest,publish_at,now))
                db.commit()
                return {"schedule_id":schedule_id,"state":"pending"}
            except Exception:
                db.rollback()
                raise

    def reschedule(self,schedule_id,new_at,expected_digest):
        if type(new_at) is not int or new_at<=int(self.clock()):
            raise ValueError("schedule_time_invalid")
        now=int(self.clock())
        with closing(self.drafts._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row=db.execute(
                    "SELECT d.text,s.expected_digest,s.state FROM scheduled_publications s "
                    "JOIN operation_drafts d ON d.id=s.draft_id WHERE s.schedule_id=?",
                    (schedule_id,)).fetchone()
                if not row or row[2]!="pending":
                    raise ValueError("schedule_not_editable")
                if row[1]!=expected_digest or self._digest(row[0])!=expected_digest:
                    raise ValueError("schedule_draft_changed")
                db.execute(
                    "UPDATE scheduled_publications SET publish_at=?,updated_at=? WHERE schedule_id=?",
                    (new_at,now,schedule_id))
                db.commit()
            except Exception:
                db.rollback()
                raise
        return {"schedule_id":schedule_id,"publish_at":new_at}

    def cancel(self,schedule_id):
        with closing(self.drafts._connect()) as db:
            changed=db.execute(
                "UPDATE scheduled_publications SET state='cancelled',updated_at=? "
                "WHERE schedule_id=? AND state='pending'",
                (int(self.clock()),schedule_id)).rowcount
            db.commit()
        return changed==1

    def list(self):
        with closing(self.drafts._connect()) as db:
            rows=db.execute(
                "SELECT schedule_id,draft_id,publish_at,state,post_id FROM "
                "scheduled_publications ORDER BY publish_at,schedule_id").fetchall()
        return [{"schedule_id":r[0],"draft_id":r[1],"publish_at":r[2],
                 "state":r[3],"provider_post_id":r[4]} for r in rows]

    def dispatch_due(self):
        """Send one due item, once; must be called by the adopted owner runner."""
        if self.publisher is None:
            return {"state":"not_connected"}
        now=int(self.clock())
        with closing(self.drafts._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row=db.execute(
                    "SELECT schedule_id,draft_id,expected_digest FROM scheduled_publications "
                    "WHERE state='pending' AND publish_at<=? "
                    "ORDER BY publish_at,schedule_id LIMIT 1",(now,)).fetchone()
                if not row:
                    db.commit()
                    return {"state":"idle"}
                schedule_id,draft_id,digest=row
                draft=db.execute("SELECT text FROM operation_drafts WHERE id=?",
                                 (draft_id,)).fetchone()
                if not draft or self._digest(draft[0])!=digest:
                    db.execute(
                        "UPDATE scheduled_publications SET state='cancelled',updated_at=? "
                        "WHERE schedule_id=?",(now,schedule_id))
                    db.commit()
                    return {"state":"draft_changed","schedule_id":schedule_id}
                db.execute(
                    "UPDATE scheduled_publications SET state='sending',updated_at=? "
                    "WHERE schedule_id=?",(now,schedule_id))
                db.commit()
            except Exception:
                db.rollback()
                raise
        # No automatic retry even if the provider threw before returning.
        try:
            result=self.publisher.perform(
                "tumblr.publish_draft",draft_id,{"expected_digest":digest})
            verified=isinstance(result,dict) and result.get("verified") is True
            post_id=result.get("provider_post_id") if verified else None
        except Exception:
            verified=False
            post_id=None
        with closing(self.drafts._connect()) as db:
            db.execute(
                "UPDATE scheduled_publications SET state=?,post_id=?,updated_at=? "
                "WHERE schedule_id=? AND state='sending'",
                ("verified" if verified else "needs_reconcile",
                 post_id,int(self.clock()),schedule_id))
            db.commit()
        return {"state":"verified" if verified else "needs_reconcile",
                "schedule_id":schedule_id, "provider_post_id":post_id}
