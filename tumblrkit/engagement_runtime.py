"""Explicit one-at-a-time Tumblr Engagement, attached to observed owner candidates.

Like/Reblog/Follow require an already observed Discovery target, owner OAuth
write consent, one bounded request budget, per-target confirmation in Assistant,
durable idempotency and official provider readback. No bulk, DM, cookies, scans,
credential logging or retries after an uncertain POST.
"""
import json
import re
import time
from contextlib import closing
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

from tumblrkit.tumblr_read import API, MAX_REPLY, normalize_blog

POST_TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,25}\Z")
BLOG_TARGET = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
POST_ID = re.compile(r"[0-9]{1,25}\Z")
KEY = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
ACTIONS = frozenset(("engagement.like", "engagement.reblog", "engagement.follow"))
RECEIPTS = """
CREATE TABLE IF NOT EXISTS engagement_receipts (
  action TEXT NOT NULL,
  target TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('reserved','sent','verified')),
  provider_post_id TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY(action,target)
);
"""


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("engagement_redirect_refused")


class EngagementTransport:
    """No caller-supplied URL, method, headers, or arbitrary provider body."""
    def post(self, path, bearer, params, budget):
        if path not in ("/v2/user/like", "/v2/user/follow") and not re.fullmatch(
                r"/v2/blog/[a-z0-9-]+\.tumblr\.com/post/reblog", path):
            raise ValueError("engagement_path_invalid")
        request=Request(API+path, data=urlencode(params).encode("utf-8"),
            headers={"Authorization":"Bearer "+bearer,
                     "Content-Type":"application/x-www-form-urlencoded",
                     "Accept":"application/json",
                     "User-Agent":"TGtumbler-Operation-Assistant/1.0"},
            method="POST")
        try:
            with build_opener(_RejectRedirect()).open(request, timeout=15) as response:
                status=response.status
                raw=response.read(MAX_REPLY+1)
        except HTTPError as exc:
            if exc.code==429:
                budget.pause_after_limit()
                raise ValueError("tumblr_rate_limited") from None
            if exc.code==401:
                raise ValueError("tumblr_auth_expired") from None
            if exc.code==403:
                raise ValueError("tumblr_permission_denied") from None
            if exc.code in (400,404,409):
                raise ValueError("engagement_provider_refused") from None
            raise ValueError("engagement_post_uncertain") from None
        except (URLError, OSError, TimeoutError):
            raise ValueError("engagement_post_uncertain") from None
        if len(raw)>MAX_REPLY:
            raise ValueError("engagement_post_uncertain")
        try:
            result=json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ValueError("engagement_post_uncertain") from None
        if not isinstance(result,dict) or result.get("meta",{}).get("status")!=status:
            raise ValueError("engagement_post_uncertain")
        return status,result


class EngagementAdapter:
    def __init__(self, owner_blog, store, token_provider, reader, budget, writer=None):
        self.owner=normalize_blog(owner_blog)
        self.store=store
        self.token_provider=token_provider
        self.reader=reader
        self.budget=budget
        self.writer=writer or EngagementTransport()

    def _lookup(self, db, action, target):
        if action not in ACTIONS or not isinstance(target,str):
            raise ValueError("engagement_target_invalid")
        if action=="engagement.follow":
            if not BLOG_TARGET.fullmatch(target) or target==self.owner:
                raise ValueError("engagement_target_invalid")
            row=db.execute("SELECT name FROM discovery_blogs WHERE name=? AND blocked=0",
                           (target,)).fetchone()
            if not row:
                raise ValueError("engagement_target_missing")
            return {"blog":target,"post_id":"","title":target,
                    "url":"https://"+target+".tumblr.com"}
        if not POST_TARGET.fullmatch(target):
            raise ValueError("engagement_target_invalid")
        row=db.execute(
            "SELECT p.blog,p.post_id,p.title,p.url FROM discovery_posts p "
            "LEFT JOIN discovery_blogs b ON b.name=p.blog "
            "WHERE p.candidate_id=? AND p.state='visible' "
            "AND COALESCE(b.blocked,0)=0",(target,)).fetchone()
        if not row or row[0]+":"+row[1]!=target:
            raise ValueError("engagement_target_missing")
        return {"blog":row[0],"post_id":row[1],"title":row[2],
                "url":row[3]}

    def preview(self, action, target):
        with closing(self.store._open()) as db:
            candidate=self._lookup(db,action,target)
        return dict(candidate)

    def _read(self, path, bearer):
        result=self.reader.get(path,bearer)
        if not isinstance(result,dict) or result.get("meta",{}).get("status")!=200:
            raise ValueError("engagement_readback_unverified")
        return result.get("response")

    @staticmethod
    def _post_info(payload, blog, post_id):
        if not isinstance(payload,dict):
            raise ValueError("engagement_readback_unverified")
        post_id_seen=str(payload.get("id_string") or payload.get("id") or "")
        name=payload.get("blog_name")
        if not name and isinstance(payload.get("blog"),dict):
            name=payload["blog"].get("name")
        if post_id_seen!=post_id or name!=blog:
            raise ValueError("engagement_readback_unverified")
        return payload

    def _verify(self, action, candidate, post_id, bearer):
        blog=candidate["blog"]
        if action=="engagement.follow":
            info=self._read("/v2/blog/"+blog+".tumblr.com/info",bearer)
            item=info.get("blog") if isinstance(info,dict) else None
            if not isinstance(item,dict) or item.get("name")!=blog or item.get("followed") is not True:
                raise ValueError("engagement_readback_unverified")
        elif action=="engagement.like":
            found=self._read("/v2/blog/"+blog+".tumblr.com/posts/"+
                             candidate["post_id"]+"?post_format=npf",bearer)
            found=self._post_info(found,blog,candidate["post_id"])
            if found.get("liked") is not True:
                raise ValueError("engagement_readback_unverified")
        else:
            if not POST_ID.fullmatch(post_id or ""):
                raise ValueError("engagement_readback_unverified")
            found=self._read("/v2/blog/"+self.owner+".tumblr.com/posts/"+
                             post_id+"?post_format=npf",bearer)
            found=self._post_info(found,self.owner,post_id)
            parent=str(found.get("parent_post_id") or "")
            if parent!=candidate["post_id"] or found.get("state")!="published":
                raise ValueError("engagement_readback_unverified")

    def _receipt(self, action, target):
        with closing(self.store._open()) as db:
            db.execute(RECEIPTS)
            row=db.execute(
                "SELECT state,provider_post_id,updated_at FROM engagement_receipts "
                "WHERE action=? AND target=?",(action,target)).fetchone()
        return row

    def _claim(self, action, target):
        with closing(self.store._open()) as db:
            db.execute(RECEIPTS)
            db.execute("BEGIN IMMEDIATE")
            self._lookup(db,action,target)
            row=db.execute(
                "SELECT state,provider_post_id,updated_at FROM engagement_receipts "
                "WHERE action=? AND target=?",(action,target)).fetchone()
            if not row:
                db.execute(
                    "INSERT INTO engagement_receipts(action,target,state,updated_at) "
                    "VALUES(?,?,'reserved',?)",(action,target,int(time.time())))
            db.commit()
        return row

    def _state(self, action, target, state, post_id=""):
        with closing(self.store._open()) as db:
            db.execute(RECEIPTS)
            db.execute("BEGIN IMMEDIATE")
            row=db.execute(
                "SELECT state,provider_post_id FROM engagement_receipts "
                "WHERE action=? AND target=?",(action,target)).fetchone()
            if not row or (row[1] and post_id and row[1]!=post_id):
                raise ValueError("engagement_reconcile_required")
            db.execute(
                "UPDATE engagement_receipts SET state=?,provider_post_id=?,updated_at=? "
                "WHERE action=? AND target=?",
                (state,post_id or row[1],int(time.time()),action,target))
            db.commit()

    def _clear_refused(self, action, target):
        with closing(self.store._open()) as db:
            db.execute(RECEIPTS)
            db.execute("DELETE FROM engagement_receipts WHERE action=? AND target=? "
                       "AND state='reserved'",(action,target))
            db.commit()

    def _result(self, action, candidate, verified_at=None, reused=False, post_id=""):
        label={"engagement.like":"いいね","engagement.reblog":"リブログ",
               "engagement.follow":"フォロー"}[action]
        return {
            "verified":True,"verification":"oauth_readback",
            "reused_existing_receipt":reused,
            "verified_at":verified_at or int(time.time()),
            "rows":[{"name":candidate["title"][:120],
                     "status":label+"：公式API再照合済み",
                     "date":str(verified_at or int(time.time())),
                     "url":("https://"+self.owner+".tumblr.com/post/"+post_id)
                           if post_id else candidate["url"]}],
        }

    def perform(self, action, target, payload):
        if action not in ACTIONS or payload not in ({},None):
            raise ValueError("engagement_action_invalid")
        candidate=self.preview(action,target)
        earlier=self._receipt(action,target)
        if earlier and earlier[0]=="verified":
            return self._result(action,candidate,earlier[2],reused=True,
                                post_id=earlier[1])
        if earlier and earlier[0]=="reserved":
            raise ValueError("engagement_reconcile_required")
        # Reserve all potential API requests together; only one explicit
        # engagement per click and none is performed by automatic crawling.
        self.budget.reserve(1 if earlier else 3)
        bearer=self.token_provider()
        if earlier and earlier[0]=="sent":
            self._verify(action,candidate,earlier[1],bearer)
            self._state(action,target,"verified",earlier[1])
            return self._result(action,candidate,post_id=earlier[1])
        blog=candidate["blog"]
        if action=="engagement.follow":
            info=self._read("/v2/blog/"+blog+".tumblr.com/info",bearer)
            details=info.get("blog") if isinstance(info,dict) else None
            if not isinstance(details,dict) or details.get("name")!=blog:
                raise ValueError("engagement_target_missing")
            if details.get("followed") is True:
                return self._result(action,candidate)
            path="/v2/user/follow"
            params={"url":"https://"+blog+".tumblr.com"}
            expected=200
        else:
            found=self._read("/v2/blog/"+blog+".tumblr.com/posts/"+
                             candidate["post_id"]+"?post_format=npf",bearer)
            post=self._post_info(found,blog,candidate["post_id"])
            reblog_key=post.get("reblog_key")
            if not isinstance(reblog_key,str) or not KEY.fullmatch(reblog_key):
                raise ValueError("engagement_target_missing")
            if action=="engagement.like":
                if post.get("liked") is True:
                    return self._result(action,candidate)
                path="/v2/user/like"
                expected=200
            else:
                path="/v2/blog/"+self.owner+".tumblr.com/post/reblog"
                expected=201
            params={"id":candidate["post_id"],"reblog_key":reblog_key}
        state=self._claim(action,target)
        if state:
            raise ValueError("engagement_reconcile_required")
        try:
            status,result=self.writer.post(path,bearer,params,self.budget)
        except ValueError as exc:
            if str(exc) in ("tumblr_auth_expired","tumblr_permission_denied",
                            "engagement_provider_refused","tumblr_rate_limited"):
                self._clear_refused(action,target)
            raise
        if status!=expected:
            raise ValueError("engagement_post_uncertain")
        new_id=""
        if action=="engagement.reblog":
            response=result.get("response")
            ident=response.get("id") if isinstance(response,dict) else None
            new_id=str(ident) if isinstance(ident,(str,int)) and not isinstance(ident,bool) else ""
            if not POST_ID.fullmatch(new_id):
                raise ValueError("engagement_post_uncertain")
        self._state(action,target,"sent",new_id)
        self._verify(action,candidate,new_id,bearer)
        self._state(action,target,"verified",new_id)
        return self._result(action,candidate,post_id=new_id)
