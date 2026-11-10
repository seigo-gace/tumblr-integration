"""Owner-scoped, resumable, non-mutating Tumblr Discovery.

One explicit source/page per action: following, followers, tag or related posts.
Uses only published official GET endpoints. No scheduler, follow/like/reblog,
bulk crawl, scraping, speculative pagination, or automatic retry.
"""
import re
from urllib.parse import urlencode, urlsplit

from tumblrkit.tumblr_read import normalize_blog, media_thumbnail, OfficialTransport
from tumblrkit.discovery_store import DiscoveryStore

SOURCE_LIMIT=10
TAG_INVALID=re.compile(r"[\x00-\x1f\x7f/?#&=<>]")

class DiscoveryEngine:
    def __init__(self, owner_blog, token_file, transport, token_provider=None):
        from pathlib import Path
        self.blog=normalize_blog(owner_blog)
        self.token_file=Path(token_file).expanduser()
        self.transport=transport
        self.token_provider=token_provider
        self.store=DiscoveryStore(self.token_file.parent/"discovery.sqlite3")

    @staticmethod
    def source_spec(target):
        if not isinstance(target,str):
            raise ValueError("discovery_target_invalid")
        target=target.strip()
        if target in ("following","フォロー先"):
            return "following","following"
        if target in ("followers","フォロワー"):
            return "followers","followers"
        if target.startswith("tag:") or target.startswith("related:"):
            mode,term=target.split(":",1)
        else:
            mode,term="tag",target
        term=term.strip()
        if not term or len(term)>64 or TAG_INVALID.search(term):
            raise ValueError("discovery_tag_invalid")
        return mode,term

    @staticmethod
    def _post(item,blocked):
        if not isinstance(item,dict):
            return None
        blog_value=item.get("blog_name")
        raw_id=item.get("id_string") or item.get("id")
        raw_url=item.get("post_url")
        if not isinstance(blog_value,str) or not isinstance(raw_url,str):
            return None
        post_id=str(raw_id)
        if not post_id.isascii() or not post_id.isdecimal() or len(post_id)>25:
            return None
        try:
            blog=normalize_blog(blog_value)
            parsed=urlsplit(raw_url)
            if (parsed.scheme!="https" or parsed.username or parsed.password
                    or not parsed.netloc or len(raw_url)>500
                    or parsed.hostname not in (blog+".tumblr.com","www.tumblr.com","tumblr.com")):
                return None
        except (TypeError,ValueError):
            return None
        if blog in blocked:
            return None
        thumbnail,media_type=media_thumbnail(item)
        tags=item.get("tags")
        if not isinstance(tags,list):
            tags=[]
        tags=[t[:100] for t in tags[:30] if isinstance(t,str)]
        text=item.get("summary") or item.get("title") or ""
        if not isinstance(text,str):
            text=""
        body=item.get("body")
        if not isinstance(body,str):
            body=""
        notes=item.get("note_count")
        if isinstance(notes,bool) or not isinstance(notes,int) or notes<0:
            notes=0
        return {
            "candidate_id":blog+":"+post_id,
            "blog":blog,"post_id":post_id,"url":raw_url,
            "title":text[:200] or "(タイトルなし)",
            "type":str(media_type or item.get("type") or "")[:40],
            "date":str(item.get("date") or "")[:60],
            "thumbnail_url":thumbnail,
            "text_excerpt":body[:8000] or text[:8000],
            "tags":tags,"note_count":notes,
        }

    @staticmethod
    def _blog(item,blocked):
        if not isinstance(item,dict):
            return None
        name=item.get("name")
        if not isinstance(name,str):
            return None
        try:
            blog=normalize_blog(name)
        except ValueError:
            return None
        if blog in blocked:
            return None
        # Do not trust a blog-supplied URL as an HTTP destination.
        return {"name":blog,"url":"https://"+blog+".tumblr.com"}

    def scan(self,target,payload=None):
        if payload not in ({},None):
            raise ValueError("discovery_payload_unsupported")
        mode,term=self.source_spec(target)
        source=mode+":"+term
        checkpoint=self.store.checkpoint(source)
        if checkpoint["complete"]:
            return self._result(source,mode,checkpoint,from_cache=True)
        offset=checkpoint["cursor"]
        if mode=="following":
            if offset>100000: raise ValueError("discovery_cursor_invalid")
            path="/v2/user/following?"+urlencode({"limit":SOURCE_LIMIT,"offset":offset})
        elif mode=="followers":
            if offset>100000: raise ValueError("discovery_cursor_invalid")
            path="/v2/blog/"+self.blog+".tumblr.com/followers?"+urlencode({
                "limit":SOURCE_LIMIT,"offset":offset})
        else:
            args={"tag":term,"limit":SOURCE_LIMIT,"filter":"text"}
            if offset>0: args["before"]=offset
            path="/v2/tagged?"+urlencode(args)
        # Shared provider-call budget is reserved before every read, even on error.
        if isinstance(self.transport,OfficialTransport):
            self.transport.budget.reserve(1)
        from tumblrkit.tumblr_read import token_from_file
        bearer=self.token_provider() if self.token_provider else token_from_file(self.token_file)
        result=self.transport.get(path,bearer)
        if (not isinstance(result,dict)
                or not isinstance(result.get("meta"),dict)
                or result["meta"].get("status")!=200):
            raise ValueError("discovery_response_unverified")
        payload=result.get("response")
        if mode in ("following","followers"):
            if not isinstance(payload,dict):
                raise ValueError("discovery_response_unverified")
            key="blogs" if mode=="following" else "users"
            count_key="total_blogs" if mode=="following" else "total_users"
            records=payload.get(key)
            count=payload.get(count_key)
            if not isinstance(records,list):
                raise ValueError("discovery_response_unverified")
            if not isinstance(count,int) or isinstance(count,bool) or count<0:
                raise ValueError("discovery_response_unverified")
            blocked=self.store.excluded_blogs()
            unique={}
            for item in records[:SOURCE_LIMIT]:
                row=self._blog(item,blocked)
                if row: unique[row["name"]]=row
            next_cursor=offset+len(records[:SOURCE_LIMIT])
            complete=next_cursor>=count or not records
            self.store.save_page(source,list(unique.values()),[],next_cursor,
                                 complete=complete,watermark=str(count))
        else:
            if not isinstance(payload,list):
                raise ValueError("discovery_response_unverified")
            blocked=self.store.excluded_blogs()
            unique={}
            for item in payload[:SOURCE_LIMIT]:
                post=self._post(item,blocked)
                if post: unique[post["candidate_id"]]=post
            timestamps=[]
            for item in payload[:SOURCE_LIMIT]:
                when=item.get("timestamp") if isinstance(item,dict) else None
                if isinstance(when,int) and when>0:
                    timestamps.append(when)
            before=min(timestamps) if timestamps else 0
            advance=bool(timestamps and before<offset) if offset else bool(timestamps)
            complete=len(payload)<SOURCE_LIMIT or not advance
            next_cursor=before if advance else offset
            self.store.save_page(source,
                                 [{"name":p["blog"],"url":"https://"+p["blog"]+".tumblr.com"}
                                  for p in unique.values()],list(unique.values()),
                                 next_cursor,complete=complete,
                                 watermark=str(max(timestamps)) if timestamps else checkpoint["watermark"])
        return self._result(source,mode,self.store.checkpoint(source))

    def _result(self,source,mode,checkpoint,from_cache=False):
        if mode in ("following","followers"):
            entries=self.store.read_blogs(source,limit=100)
            rows=[{"blog":entry["name"],"title":entry["name"],"type":"blog","date":"",
                   "thumbnail_url":"","url":entry["url"]} for entry in entries]
        else:
            posts=self.store.read_candidates(source,limit=100)
            rows=[{"blog":p["blog"],"candidate_id":p["candidate_id"],"title":p["title"],"type":p["type"],"date":p["date"],
                   "thumbnail_url":p["thumbnail_url"],"url":p["url"]} for p in posts]
        return {"verified":True,"rows":rows,"total":len(rows),
                "source":source,"cursor":checkpoint["cursor"],
                "complete":checkpoint["complete"],"from_cache":from_cache}

    def recheck_candidate(self,candidate_id):
        """Explicit one-post inspection only; never periodic availability probing.

        404 from the official exact-id endpoint may mean deleted or non-public.
        Network failures and 403 are not deletion evidence.
        """
        record=self.store.candidate(candidate_id)
        blog=normalize_blog(record["blog"])
        post_id=record["post_id"]
        if not post_id.isascii() or not post_id.isdecimal():
            raise ValueError("discovery_candidate_id_invalid")
        if isinstance(self.transport,OfficialTransport):
            self.transport.budget.reserve(1)
        from tumblrkit.tumblr_read import token_from_file
        bearer=self.token_provider() if self.token_provider else token_from_file(self.token_file)
        path="/v2/blog/"+blog+".tumblr.com/posts?"+urlencode({
            "id":post_id,"npf":"true","limit":1})
        try:
            result=self.transport.get(path,bearer)
        except ValueError as exc:
            if str(exc)=="tumblr_post_unavailable":
                self.store.mark_unavailable(candidate_id)
                return {"verified":True,"candidate_id":candidate_id,"state":"unavailable"}
            raise
        if (not isinstance(result,dict) or result.get("meta",{}).get("status")!=200):
            raise ValueError("discovery_candidate_check_unverified")
        posts=result.get("response",{}).get("posts") if isinstance(result.get("response"),dict) else None
        if not isinstance(posts,list):
            raise ValueError("discovery_candidate_check_unverified")
        if not any(str(p.get("id_string") or p.get("id"))==post_id for p in posts if isinstance(p,dict)):
            self.store.mark_unavailable(candidate_id)
            return {"verified":True,"candidate_id":candidate_id,"state":"unavailable"}
        return {"verified":True,"candidate_id":candidate_id,"state":"visible"}
