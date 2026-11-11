"""Owner-confirmed, at-most-once Tumblr NPF text publication.

No background worker, automatic retries, extra databases, or provider calls in
tests/deploy. A locally saved immutable draft ID is required. The exact owner
blog and official API hostname are fixed in the existing OAuth config.
"""
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from tumblrkit.drafts import DraftStore
from tumblrkit.tumblr_read import API, OfficialTransport, normalize_blog

POST_ID = re.compile(r"[0-9]{1,25}\Z")
PUBLISH_SCHEMA = """
CREATE TABLE IF NOT EXISTS tumblr_publish_receipts(
 draft_id TEXT PRIMARY KEY,
 digest TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('reserved','sent','verified')),
 post_id TEXT,
 updated_at INTEGER NOT NULL
);
"""


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ValueError("tumblr_publish_redirect_refused")


class PostTransport:
    """One bounded official NPF POST; never follow a Bearer-bearing redirect."""
    def post_json(self, path, bearer, body, budget):
        if not re.fullmatch(r"/v2/blog/[a-z0-9-]+\.tumblr\.com/posts", path):
            raise ValueError("tumblr_publish_path_invalid")
        request=Request(API+path,
            data=json.dumps(body,ensure_ascii=False).encode("utf-8"),
            headers={"Authorization":"Bearer "+bearer,
                     "User-Agent":"TGtumbler-Operation-Assistant/1.0",
                     "Content-Type":"application/json",
                     "Accept":"application/json"},method="POST")
        try:
            with build_opener(RejectRedirects()).open(request,timeout=15) as response:
                status=response.status
                raw=response.read(1024*1024+1)
        except HTTPError as exc:
            if exc.code==429:
                budget.pause_after_limit()
                raise ValueError("tumblr_rate_limited") from None
            if exc.code==401:raise ValueError("tumblr_auth_expired") from None
            if exc.code==403:raise ValueError("tumblr_permission_denied") from None
            if exc.code==400:raise ValueError("tumblr_publish_rejected") from None
            # HTTP 5xx can be uncertain after a remote state transition.
            raise ValueError("tumblr_publish_uncertain") from None
        except (URLError,OSError,TimeoutError):
            raise ValueError("tumblr_publish_uncertain") from None
        if status!=201 or len(raw)>1024*1024:
            raise ValueError("tumblr_publish_uncertain")
        try:
            payload=json.loads(raw.decode("utf-8"))
        except (UnicodeError,ValueError):
            raise ValueError("tumblr_publish_uncertain") from None
        return payload


class PublisherAdapter:
    def __init__(self, owner_blog, draft_store, token_provider,
                 read_transport=None, write_transport=None, budget=None):
        self.blog=normalize_blog(owner_blog)
        self.drafts=draft_store
        self.token_provider=token_provider
        if budget is None:
            raise ValueError("tumblr_shared_api_budget_required")
        self.budget=budget
        self.reader=read_transport or OfficialTransport(self.budget)
        self.writer=write_transport or PostTransport()

    def _claim(self, draft_id, digest):
        with closing(self.drafts._connect()) as db:
            db.execute(PUBLISH_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            try:
                row=db.execute("SELECT digest,state,post_id FROM tumblr_publish_receipts "
                               "WHERE draft_id=?",(draft_id,)).fetchone()
                if not row:
                    db.execute("INSERT INTO tumblr_publish_receipts "
                               "(draft_id,digest,state,updated_at) VALUES(?,?,'reserved',?)",
                               (draft_id,digest,int(time.time())))
                    state,post_id="reserved_new",None
                else:
                    if row[0]!=digest:
                        raise ValueError("tumblr_publish_draft_changed")
                    state,post_id=row[1],row[2]
                db.commit()
                return state,post_id
            except Exception:
                db.rollback()
                raise

    def _set_state(self,draft_id,state,post_id=None):
        with closing(self.drafts._connect()) as db:
            db.execute(PUBLISH_SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            row=db.execute("SELECT post_id,state FROM tumblr_publish_receipts "
                           "WHERE draft_id=?",(draft_id,)).fetchone()
            if not row:
                db.rollback()
                raise ValueError("tumblr_publish_receipt_missing")
            if row[0] and post_id and row[0]!=post_id:
                db.rollback()
                raise ValueError("tumblr_publish_post_mismatch")
            db.execute("UPDATE tumblr_publish_receipts SET state=?,post_id=?,updated_at=? "
                       "WHERE draft_id=?",(state,post_id or row[0],int(time.time()),draft_id))
            db.commit()

    def _readback(self,post_id,text,bearer):
        if not isinstance(post_id,str) or not POST_ID.fullmatch(post_id):
            raise ValueError("tumblr_publish_post_id_invalid")
        raw=self.reader.get(
            "/v2/blog/"+self.blog+".tumblr.com/posts/"+post_id+"?npf=true",
            bearer)
        if (not isinstance(raw,dict) or raw.get("meta",{}).get("status")!=200
                or not isinstance(raw.get("response"),dict)):
            raise ValueError("tumblr_publish_readback_unverified")
        post=raw["response"]
        ident=str(post.get("id_string") or post.get("id") or "")
        blocks=post.get("content")
        if (ident!=post_id or post.get("blog_name")!=self.blog
                or post.get("state")!="published"
                or not isinstance(blocks,list)
                or not any(isinstance(block,dict) and block.get("type")=="text"
                           and block.get("text")==text for block in blocks)):
            raise ValueError("tumblr_publish_readback_unverified")
        return {
            "verified":True, "provider_post_id":post_id,
            "rows":[{"title":text[:100], "status":"published",
                     "date":str(post.get("date") or "")[:50],
                     "url":"https://"+self.blog+".tumblr.com/post/"+post_id}],
        }

    def perform(self,action,target,payload):
        if action!="tumblr.publish_draft":
            raise ValueError("tumblr_publish_action_invalid")
        if payload not in ({},None):
            raise ValueError("tumblr_publish_payload_unsupported")
        draft=self.drafts.get(target)
        content=draft["text"]
        if not content or len(content)>2000:
            raise ValueError("tumblr_publish_text_invalid")
        digest=hashlib.sha256(content.encode("utf-8")).hexdigest()
        # Reserve for both official calls up front, even if a later response
        # fails. The safe durable state is committed before any POST attempt.
        with closing(self.drafts._connect()) as db:
            db.execute(PUBLISH_SCHEMA)
            existing=db.execute("SELECT state FROM tumblr_publish_receipts "
                                "WHERE draft_id=?",(target,)).fetchone()
        self.budget.reserve(1 if existing else 2)
        bearer=self.token_provider()
        state,post_id=self._claim(target,digest)
        if state=="reserved":
            raise ValueError("tumblr_publish_reconcile_required")
        if state=="reserved_new":
            body={"state":"published","content":[{"type":"text","text":content}],
                  "send_to_twitter":False}
            try:
                response=self.writer.post_json(
                    "/v2/blog/"+self.blog+".tumblr.com/posts",bearer,body,self.budget)
            except ValueError as exc:
                # Explicit HTTP 4xx rejection means no post was created.
                if str(exc) in ("tumblr_rate_limited","tumblr_auth_expired",
                                "tumblr_permission_denied","tumblr_publish_rejected"):
                    with closing(self.drafts._connect()) as db:
                        db.execute("DELETE FROM tumblr_publish_receipts "
                                   "WHERE draft_id=? AND state='reserved'",(target,))
                        db.commit()
                raise
            post=response.get("response") if isinstance(response,dict) else None
            post_id=post.get("id") if isinstance(post,dict) else None
            if (not isinstance(response,dict)
                    or response.get("meta",{}).get("status")!=201
                    or not isinstance(post_id,str) or not POST_ID.fullmatch(post_id)):
                raise ValueError("tumblr_publish_uncertain")
            self._set_state(target,"sent",post_id)
        outcome=self._readback(post_id,content,bearer)
        self._set_state(target,"verified",post_id)
        return outcome
