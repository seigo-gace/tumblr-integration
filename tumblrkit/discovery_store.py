"""Durable Discovery candidates and cursor store. Never contains OAuth credentials or media bytes.

Owned by the existing TGtumbler discovery module. Separate concerns:
- API safety budget counts attempts before provider calls.
- This store commits page/cursor only after verified response processing.
- Library, Viewer and Analytics may read its stable IDs in later modules.
"""
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_sources(
  source_key TEXT PRIMARY KEY,
  cursor INTEGER NOT NULL DEFAULT 0,
  watermark TEXT NOT NULL DEFAULT '',
  complete INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_blogs(
  name TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  source TEXT NOT NULL,
  blocked INTEGER NOT NULL DEFAULT 0,
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_posts(
  candidate_id TEXT PRIMARY KEY,
  blog TEXT NOT NULL,
  post_id TEXT NOT NULL,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT NOT NULL,
  post_type TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  thumbnail_url TEXT NOT NULL,
  text_excerpt TEXT NOT NULL,
  tags_json TEXT NOT NULL,
  media_json TEXT NOT NULL DEFAULT '[]',
  note_count INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'visible',
  first_seen INTEGER NOT NULL,
  last_seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_blog_sources(
  name TEXT NOT NULL, source TEXT NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY(name,source)
);
CREATE TABLE IF NOT EXISTS discovery_post_sources(
  candidate_id TEXT NOT NULL, source TEXT NOT NULL, last_seen INTEGER NOT NULL,
  PRIMARY KEY(candidate_id,source)
);
CREATE INDEX IF NOT EXISTS discovery_posts_by_source
ON discovery_posts(source,last_seen);
"""

class DiscoveryStore:
    def __init__(self, path, clock=None):
        self.path=Path(path).expanduser()
        self.clock=clock or time.time

    def _open(self):
        folder=self.path.parent
        if (not folder.is_dir() or folder.is_symlink()
                or folder.stat().st_uid!=os.getuid() or folder.stat().st_mode & 0o077):
            raise ValueError("discovery_directory_unsafe")
        if self.path.is_symlink():
            raise ValueError("discovery_store_symlink")
        if not self.path.exists():
            try:
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|
                           getattr(os,"O_NOFOLLOW",0),0o600)
                os.close(fd)
            except FileExistsError:
                pass
        info=self.path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("discovery_store_unsafe")
        db=sqlite3.connect(self.path,timeout=4,isolation_level=None)
        db.execute("PRAGMA busy_timeout=4000")
        db.execute("PRAGMA journal_mode=DELETE")
        db.executescript(SCHEMA)
        columns={row[1] for row in db.execute("PRAGMA table_info(discovery_posts)")}
        if "media_json" not in columns:
            db.execute("ALTER TABLE discovery_posts ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]'")
        return db

    def checkpoint(self,source):
        with closing(self._open()) as db:
            row=db.execute("SELECT cursor,watermark,complete FROM discovery_sources WHERE source_key=?",
                           (source,)).fetchone()
        return {"cursor":row[0],"watermark":row[1],"complete":bool(row[2])} if row else {
            "cursor":0,"watermark":"","complete":False}

    def save_page(self,source,blogs,posts,next_cursor,complete=False,watermark=""):
        if not isinstance(next_cursor,int) or next_cursor<0 or next_cursor>100000:
            raise ValueError("discovery_cursor_invalid")
        now=int(self.clock())
        with closing(self._open()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                for blog in blogs:
                    db.execute("INSERT INTO discovery_blogs(name,url,source,first_seen,last_seen)"
                               " VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
                               " last_seen=excluded.last_seen,url=excluded.url,source=excluded.source",
                               (blog["name"],blog["url"],source,now,now))
                    db.execute("INSERT INTO discovery_blog_sources(name,source,last_seen) VALUES(?,?,?) "
                               "ON CONFLICT(name,source) DO UPDATE SET last_seen=excluded.last_seen",
                               (blog["name"],source,now))
                for post in posts:
                    # Once the owner excludes a blog, never create more candidates for it.
                    blocked=db.execute("SELECT blocked FROM discovery_blogs WHERE name=?",
                                       (post["blog"],)).fetchone()
                    if blocked and blocked[0]:
                        continue
                    db.execute("INSERT INTO discovery_posts(candidate_id,blog,post_id,source,url,title,"
                               "post_type,posted_at,thumbnail_url,text_excerpt,tags_json,media_json,note_count,first_seen,last_seen)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET"
                               " last_seen=excluded.last_seen,url=excluded.url,title=excluded.title,"
                               " post_type=excluded.post_type,posted_at=excluded.posted_at,"
                               " thumbnail_url=excluded.thumbnail_url,text_excerpt=excluded.text_excerpt,"
                               " tags_json=excluded.tags_json,media_json=excluded.media_json,"
                               " note_count=excluded.note_count,state='visible'",
                               (post["candidate_id"],post["blog"],post["post_id"],source,post["url"],
                                post["title"],post["type"],post["date"],post["thumbnail_url"],
                                post["text_excerpt"],json.dumps(post["tags"],ensure_ascii=False),
                                json.dumps(post.get("media_refs",[]),ensure_ascii=False),
                                post["note_count"],now,now))
                    db.execute("INSERT INTO discovery_post_sources(candidate_id,source,last_seen) VALUES(?,?,?) "
                               "ON CONFLICT(candidate_id,source) DO UPDATE SET last_seen=excluded.last_seen",
                               (post["candidate_id"],source,now))
                db.execute("INSERT INTO discovery_sources(source_key,cursor,watermark,complete,updated_at)"
                           " VALUES(?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET"
                           " cursor=excluded.cursor,watermark=excluded.watermark,"
                           " complete=excluded.complete,updated_at=excluded.updated_at",
                           (source,next_cursor,watermark,int(bool(complete)),now))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def exclude_blog(self,name):
        from tumblrkit.tumblr_read import normalize_blog
        blog=normalize_blog(name)
        now=int(self.clock())
        with closing(self._open()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO discovery_blogs(name,url,source,blocked,first_seen,last_seen)"
                           " VALUES(?,?,?,1,?,?) ON CONFLICT(name) DO UPDATE SET blocked=1",
                           (blog,"https://"+blog+".tumblr.com","excluded",now,now))
                db.execute("UPDATE discovery_posts SET state='excluded' WHERE blog=?",(blog,))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def read_candidates(self,source=None,limit=100):
        if not 1<=limit<=100: raise ValueError("discovery_limit_invalid")
        with closing(self._open()) as db:
            if source:
                posts=db.execute("SELECT candidate_id,blog,post_id,url,title,post_type,posted_at,"
                                 "thumbnail_url,text_excerpt,tags_json,media_json,note_count,state FROM discovery_posts"
                                 " WHERE candidate_id IN (SELECT candidate_id FROM discovery_post_sources WHERE source=?)"
                                 " AND state='visible' AND blog NOT IN"
                                 " (SELECT name FROM discovery_blogs WHERE blocked=1)"
                                 " ORDER BY last_seen DESC LIMIT ?",(source,limit)).fetchall()
            else:
                posts=db.execute("SELECT candidate_id,blog,post_id,url,title,post_type,posted_at,"
                                 "thumbnail_url,text_excerpt,tags_json,media_json,note_count,state FROM discovery_posts"
                                 " WHERE state='visible' AND blog NOT IN"
                                 " (SELECT name FROM discovery_blogs WHERE blocked=1)"
                                 " ORDER BY last_seen DESC LIMIT ?",(limit,)).fetchall()
        columns=("candidate_id","blog","post_id","url","title","type","date",
                 "thumbnail_url","text_excerpt","tags_json","media_json","note_count","state")
        return [dict(zip(columns,item)) for item in posts]

    def mark_unavailable(self,candidate_id):
        # Only when the official API explicitly reports deletion/non-public state
        # for this exact post; absence on a partial page is not evidence.
        with closing(self._open()) as db:
            db.execute("UPDATE discovery_posts SET state='unavailable' WHERE candidate_id=?",
                       (candidate_id,))

    def excluded_blogs(self):
        with closing(self._open()) as db:
            rows=db.execute("SELECT name FROM discovery_blogs WHERE blocked=1").fetchall()
        return {row[0] for row in rows}

    def read_blogs(self,source=None,limit=100):
        with closing(self._open()) as db:
            if source:
                rows=db.execute("SELECT b.name,b.url,? FROM discovery_blogs b"
                                " WHERE b.name IN (SELECT name FROM discovery_blog_sources WHERE source=?)"
                                " AND b.blocked=0 ORDER BY b.last_seen DESC LIMIT ?",
                                (source,source,limit)).fetchall()
            else:
                rows=db.execute("SELECT name,url,source FROM discovery_blogs WHERE blocked=0"
                                " ORDER BY last_seen DESC LIMIT ?",(limit,)).fetchall()
        return [{"name":name,"url":url,"source":src} for name,url,src in rows]

    def candidate(self,candidate_id):
        if not isinstance(candidate_id,str) or len(candidate_id)>100:
            raise ValueError("discovery_candidate_id_invalid")
        with closing(self._open()) as db:
            row=db.execute("SELECT candidate_id,blog,post_id,url,title,state "
                           "FROM discovery_posts WHERE candidate_id=?",
                           (candidate_id,)).fetchone()
        if row is None:
            raise ValueError("discovery_candidate_not_found")
        return dict(zip(("candidate_id","blog","post_id","url","title","state"),row))
