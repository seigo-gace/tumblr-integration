"""Owner-only, on-demand official Tumblr follower snapshots.

No background scanning and no invented follower counts. A fresh count comes
only from the authorized owner's /v2/user/info response, using the already
configured OAuth transport and persistent API budget. History stays in the
existing private Discovery/Library SQLite file.
"""
from datetime import datetime, timezone
import time

from tumblrkit.tumblr_read import OfficialTransport, normalize_blog, token_from_file


SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics_follower_snapshots(
 blog TEXT NOT NULL,
 day TEXT NOT NULL,
 followers INTEGER NOT NULL CHECK(followers>=0),
 observed_at INTEGER NOT NULL,
 PRIMARY KEY(blog,day)
);
"""


class FollowerAnalytics:
    def __init__(self, reader, discovery_store, *, clock=None):
        self.reader=reader
        self.store=discovery_store
        self.clock=clock or time.time

    def snapshot(self):
        """One explicitly requested identity GET, never a batch or retry."""
        reader=self.reader
        if reader is None:
            raise ValueError("analytics_oauth_not_connected")
        if isinstance(reader.transport, OfficialTransport):
            reader.transport.budget.reserve(1)
        token=(reader.token_provider() if reader.token_provider
               else token_from_file(reader.token_file))
        response=reader.transport.get("/v2/user/info",token)
        if not isinstance(response,dict) or not isinstance(response.get("meta"),dict) or response["meta"].get("status")!=200:
            raise ValueError("analytics_owner_readback_invalid")
        data=response.get("response")
        user=data.get("user") if isinstance(data,dict) else None
        blogs=user.get("blogs") if isinstance(user,dict) else None
        if not isinstance(blogs,list):
            raise ValueError("analytics_owner_readback_invalid")
        matches=[]
        for entry in blogs:
            if not isinstance(entry,dict):
                continue
            try:
                owner=normalize_blog(entry.get("name"))
            except ValueError:
                continue
            if owner==reader.blog:
                matches.append(entry)
        if len(matches)!=1:
            raise ValueError("analytics_owner_not_verified")
        followers=matches[0].get("followers")
        if type(followers) is not int or not 0<=followers<=1000000000:
            raise ValueError("analytics_followers_not_reported")
        observed=int(self.clock())
        day=datetime.fromtimestamp(observed,timezone.utc).strftime("%Y-%m-%d")
        db=self.store._open()
        try:
            with db:
                db.executescript(SCHEMA)
                db.execute(
                    "INSERT INTO analytics_follower_snapshots VALUES(?,?,?,?) "
                    "ON CONFLICT(blog,day) DO UPDATE SET followers=excluded.followers,"
                    "observed_at=excluded.observed_at",
                    (reader.blog,day,followers,observed))
        finally:
            db.close()
        return {"verified":True,"blog":reader.blog,"followers":followers,
                "observed_at":observed,"basis":"official_owner_oauth_user_info"}

    def history(self,limit=90):
        if type(limit) is not int or not 1<=limit<=365:
            raise ValueError("analytics_history_limit_invalid")
        if self.reader is None:
            raise ValueError("analytics_oauth_not_connected")
        db=self.store._open()
        try:
            db.executescript(SCHEMA)
            rows=db.execute(
                "SELECT day,followers,observed_at FROM analytics_follower_snapshots "
                "WHERE blog=? ORDER BY day DESC LIMIT ?",
                (self.reader.blog,limit)).fetchall()
        finally:
            db.close()
        return {"blog":self.reader.blog,"source":"owner_private_verified_snapshots",
                "rows":[{"day":r[0],"followers":r[1],"observed_at":r[2]}
                        for r in reversed(rows)],"provider_calls":0}
