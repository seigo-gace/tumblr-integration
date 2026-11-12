"""Confirmed owner post edits/deletes using the existing private drafts database.

The action input is never an arbitrary Tumblr post ID: it is an immutable
local published draft ID (plus a separately saved revision for edits). The
result is verified with a separate official read. Unknown writes are not
retried, even across restart.
"""
import hashlib
import json
import re
import time
from contextlib import closing

from tumblrkit.publisher_runtime import POST_ID, PUBLISH_SCHEMA

HEX32 = re.compile(r"[a-f0-9]{32}\Z")
LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tumblr_post_mutations(
 operation_key TEXT PRIMARY KEY,
 source_draft_id TEXT NOT NULL,
 revision_draft_id TEXT,
 action TEXT NOT NULL CHECK(action IN ('edit','delete')),
 post_id TEXT NOT NULL,
 digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','sent','verified')),
 created_at INTEGER NOT NULL
);
"""


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PublishedPostLifecycle:
    def __init__(self, publisher):
        self.publisher=publisher
        self.drafts=publisher.drafts

    @staticmethod
    def _parse(action,target):
        if action=="tumblr.edit_post":
            if not isinstance(target,str):
                raise ValueError("tumblr_post_target_invalid")
            values=target.split(":")
            if (len(values)!=2 or not all(HEX32.fullmatch(v) for v in values)
                    or values[0]==values[1]):
                raise ValueError("tumblr_post_target_invalid")
            return "edit",values[0],values[1]
        if action=="tumblr.delete_post":
            if not isinstance(target,str) or not HEX32.fullmatch(target):
                raise ValueError("tumblr_post_target_invalid")
            return "delete",target,None
        raise ValueError("tumblr_post_action_invalid")

    @staticmethod
    def _schema(db):
        db.execute(PUBLISH_SCHEMA)
        db.execute(LIFECYCLE_SCHEMA)

    @staticmethod
    def _db_snapshot(db,source,revision=None,op_key=None):
        original=db.execute(
            "SELECT p.digest,p.state,p.post_id,d.text FROM tumblr_publish_receipts p "
            "JOIN operation_drafts d ON d.id=p.draft_id WHERE p.draft_id=?",
            (source,)).fetchone()
        if (not original or original[1]!="verified"
                or not isinstance(original[2],str)
                or not POST_ID.fullmatch(original[2])):
            raise ValueError("tumblr_post_not_verified")
        post_id=original[2]
        rows=db.execute(
            "SELECT operation_key,revision_draft_id,action,state,post_id "
            "FROM tumblr_post_mutations WHERE source_draft_id=? ORDER BY rowid ASC",
            (source,)).fetchall()
        for key,_,action,state,_ in rows:
            if action=="delete" and state=="verified" and key!=op_key:
                raise ValueError("tumblr_post_already_deleted")
            if state in ("reserved","sent") and key!=op_key:
                raise ValueError("tumblr_post_reconcile_required")
        current=original[3]
        for key,edit_id,action,state,receipt_post_id in rows:
            if receipt_post_id!=post_id:
                raise ValueError("tumblr_post_identity_conflict")
            if action=="edit" and state=="verified":
                result=db.execute("SELECT text FROM operation_drafts WHERE id=?",
                                  (edit_id,)).fetchone()
                if not result:
                    raise ValueError("tumblr_post_revision_missing")
                current=result[0]
        next_text=None
        if revision is not None:
            result=db.execute("SELECT text FROM operation_drafts WHERE id=?",
                              (revision,)).fetchone()
            if not result:
                raise ValueError("tumblr_post_revision_missing")
            next_text=result[0]
            if not next_text or len(next_text)>2000 or next_text==current:
                raise ValueError("tumblr_post_revision_invalid")
        return post_id,current,next_text

    @staticmethod
    def _approval(action,source,revision,post_id,current,new_text):
        body=json.dumps([action,source,revision,post_id,current,new_text],
                        ensure_ascii=False,separators=(",",":"))
        return _hash(body)

    def preview(self,action,target):
        kind,source,revision=self._parse(action,target)
        key=kind+":"+(revision or source)
        with closing(self.drafts._connect()) as db:
            self._schema(db)
            post_id,current,next_text=self._db_snapshot(db,source,revision,key)
        fingerprint=self._approval(kind,source,revision,post_id,current,next_text)
        return {"action":kind,"source":source,"revision":revision,
                "post_id":post_id,"current_text":current,"new_text":next_text,
                "approval":fingerprint}

    def _claim(self,action,source,revision,approved):
        key=action+":"+(revision or source)
        with closing(self.drafts._connect()) as db:
            self._schema(db)
            db.execute("BEGIN IMMEDIATE")
            existing=db.execute(
                "SELECT state,digest,post_id FROM tumblr_post_mutations "
                "WHERE operation_key=?",(key,)).fetchone()
            if existing and existing[0]=="verified":
                if approved!=existing[1]:
                    raise ValueError("tumblr_post_approval_stale")
                latest=db.execute(
                    "SELECT operation_key FROM tumblr_post_mutations "
                    "WHERE source_draft_id=? ORDER BY rowid DESC LIMIT 1",
                    (source,)).fetchone()
                if not latest or latest[0]!=key:
                    raise ValueError("tumblr_post_approval_stale")
                revised=db.execute(
                    "SELECT text FROM operation_drafts WHERE id=?",(revision,)
                ).fetchone() if revision else None
                db.commit()
                return key,"verified",existing[2],None,(revised[0] if revised else None)
            post_id,current,new_text=self._db_snapshot(db,source,revision,key)
            fingerprint=self._approval(action,source,revision,post_id,current,new_text)
            if approved!=fingerprint:
                raise ValueError("tumblr_post_approval_stale")
            row=db.execute(
                "SELECT state,digest FROM tumblr_post_mutations WHERE operation_key=?",
                (key,)).fetchone()
            if row:
                if row[1]!=fingerprint:
                    raise ValueError("tumblr_post_approval_stale")
                state=row[0]
            else:
                db.execute(
                    "INSERT INTO tumblr_post_mutations "
                    "(operation_key,source_draft_id,revision_draft_id,action,post_id,"
                    "digest,state,created_at) VALUES(?,?,?,?,?,?,'reserved',?)",
                    (key,source,revision,action,post_id,fingerprint,int(time.time())))
                state="new"
            db.commit()
        return key,state,post_id,current,new_text

    def _change_state(self,key,state):
        with closing(self.drafts._connect()) as db:
            self._schema(db)
            db.execute("BEGIN IMMEDIATE")
            row=db.execute(
                "SELECT state FROM tumblr_post_mutations WHERE operation_key=?",
                (key,)).fetchone()
            if row is None:
                raise ValueError("tumblr_post_receipt_missing")
            db.execute("UPDATE tumblr_post_mutations SET state=? WHERE operation_key=?",
                       (state,key))
            db.commit()

    def _cancel_unwritten(self,key):
        with closing(self.drafts._connect()) as db:
            db.execute("DELETE FROM tumblr_post_mutations "
                       "WHERE operation_key=? AND state='reserved'",(key,))
            db.commit()

    def _get_post(self,post_id,bearer):
        return self.publisher.reader.get(
            "/v2/blog/"+self.publisher.blog+".tumblr.com/posts/"+
            post_id+"?post_format=npf",bearer)

    def _verify_text(self,post_id,expected,bearer):
        raw=self._get_post(post_id,bearer)
        post=raw.get("response") if isinstance(raw,dict) else None
        if (not isinstance(post,dict) or not isinstance(raw.get("meta"),dict)
                or raw["meta"].get("status")!=200):
            raise ValueError("tumblr_post_readback_unverified")
        owner=post.get("blog_name")
        if not owner and isinstance(post.get("blog"),dict):
            owner=post["blog"].get("name")
        content=post.get("content")
        if (str(post.get("id_string") or post.get("id") or "")!=post_id
                or owner!=self.publisher.blog or post.get("state")!="published"
                or not isinstance(content,list) or len(content)!=1
                or not isinstance(content[0],dict)
                or content[0].get("type")!="text"
                or content[0].get("text")!=expected):
            raise ValueError("tumblr_post_readback_unverified")
        return post

    def _verify_deleted(self,post_id,bearer):
        try:
            self._get_post(post_id,bearer)
        except ValueError as exc:
            if str(exc)=="tumblr_post_unavailable":
                return
            raise
        raise ValueError("tumblr_post_readback_unverified")

    def _result(self,action,post_id,text):
        deleted=action=="delete"
        return {
            "verified":True,"provider_post_id":post_id,
            "rows":[{"title":("削除済み" if deleted else text[:100]),
                     "status":("deleted" if deleted else "edited"),
                     "date":"","url":("" if deleted else
                         "https://"+self.publisher.blog+".tumblr.com/post/"+post_id)}],
        }

    def reconcile(self,source):
        """Read-only reconciliation of a previously uncertain edit/delete.

        Never send an additional PUT or DELETE. The verified provider state
        must match the exact locally approved content before changing ledger.
        """
        if not isinstance(source,str) or not HEX32.fullmatch(source):
            raise ValueError("tumblr_post_target_invalid")
        with closing(self.drafts._connect()) as db:
            self._schema(db)
            row=db.execute(
                "SELECT operation_key,action,post_id,revision_draft_id,state "
                "FROM tumblr_post_mutations WHERE source_draft_id=? "
                "AND state IN ('reserved','sent') ORDER BY rowid DESC LIMIT 1",
                (source,)).fetchone()
            if row is None:
                raise ValueError("tumblr_post_reconcile_missing")
            key,kind,post_id,revision,state=row
            if kind not in ("edit","delete") or not POST_ID.fullmatch(post_id):
                raise ValueError("tumblr_post_identity_conflict")
            new_text=None
            if kind=="edit":
                revised=db.execute(
                    "SELECT text FROM operation_drafts WHERE id=?",
                    (revision,)).fetchone()
                if not revised:
                    raise ValueError("tumblr_post_revision_missing")
                new_text=revised[0]
        self.publisher.budget.reserve(1)
        bearer=self.publisher.token_provider()
        if kind=="edit":
            self._verify_text(post_id,new_text,bearer)
        else:
            self._verify_deleted(post_id,bearer)
        self._change_state(key,"verified")
        return self._result(kind,post_id,new_text)

    def perform(self,action,target,payload):
        kind,source,revision=self._parse(action,target)
        if (not isinstance(payload,dict) or
                set(payload)!={"expected_digest"} or
                not isinstance(payload.get("expected_digest"),str) or
                not re.fullmatch(r"[0-9a-f]{64}",payload["expected_digest"])):
            raise ValueError("tumblr_post_approval_required")
        approved=payload["expected_digest"]
        # Establish OAuth and available shared-call budget before creating a
        # durable write reservation. A missing key/quota cannot lock a draft
        # that has never been sent to Tumblr.
        key=kind+":"+(revision or source)
        with closing(self.drafts._connect()) as db:
            self._schema(db)
            previous=db.execute(
                "SELECT state FROM tumblr_post_mutations WHERE operation_key=?",
                (key,)).fetchone()
        if previous and previous[0]=="reserved":
            raise ValueError("tumblr_post_reconcile_required")
        self.publisher.budget.reserve(1 if previous else 3)
        bearer=self.publisher.token_provider()
        claim=self._claim(kind,source,revision,approved)
        key,state,post_id,old_text,new_text=claim
        if state=="reserved":
            raise ValueError("tumblr_post_reconcile_required")
        if state in ("sent","verified"):
            if kind=="edit":
                self._verify_text(post_id,new_text,bearer)
            else:
                self._verify_deleted(post_id,bearer)
            if state=="sent":
                self._change_state(key,"verified")
            return self._result(kind,post_id,new_text)
        # Exactly one remote mutation per new operation, at most three calls.
        try:
            self._verify_text(post_id,old_text,bearer)
        except ValueError:
            # No remote side effect yet; release the local reservation.
            self._cancel_unwritten(key)
            raise ValueError("tumblr_post_external_changed") from None
        path="/v2/blog/"+self.publisher.blog+".tumblr.com/"
        if kind=="edit":
            method="PUT"
            path+="posts/"+post_id
            body={"state":"published",
                  "content":[{"type":"text","text":new_text}],
                  "send_to_twitter":False}
        else:
            method="POST"
            path+="post/delete"
            body={"id":post_id}
        try:
            response=self.publisher.writer.mutate_json(
                method,path,bearer,body,self.publisher.budget)
        except ValueError as exc:
            if str(exc) in ("tumblr_rate_limited","tumblr_auth_expired",
                             "tumblr_permission_denied","tumblr_mutation_rejected"):
                self._cancel_unwritten(key)
            # Timeouts/5xx remain reserved: never resubmit an uncertain write.
            raise
        if kind=="edit":
            payload_post=response.get("response")
            ident=(payload_post.get("id") if isinstance(payload_post,dict) else None)
            if str(ident or "")!=post_id:
                # Successful status but inconsistent provider receipt: unknown.
                raise ValueError("tumblr_mutation_uncertain")
        self._change_state(key,"sent")
        if kind=="edit":
            self._verify_text(post_id,new_text,bearer)
        else:
            self._verify_deleted(post_id,bearer)
        self._change_state(key,"verified")
        return self._result(kind,post_id,new_text)
