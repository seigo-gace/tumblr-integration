"""Library/Viewer for verified Discovery records; no external network or media downloads.

Reuses the existing owner-private Discovery SQLite DB and stable candidate IDs.
Flags are independent of discovery metadata, so rescans never reset user choices.
"""
import json
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import urlsplit

from tumblrkit.discovery_store import DiscoveryStore

IDENT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,25}\Z")
SAFE_MEDIA_HOST = re.compile(r"(?:[a-z0-9-]+\.)?media\.tumblr\.com\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS library_state(
 candidate_id TEXT PRIMARY KEY,
 seen INTEGER NOT NULL DEFAULT 0 CHECK(seen IN(0,1)),
 favorite INTEGER NOT NULL DEFAULT 0 CHECK(favorite IN(0,1)),
 watch_later INTEGER NOT NULL DEFAULT 0 CHECK(watch_later IN(0,1)),
 last_media_index INTEGER NOT NULL DEFAULT 0 CHECK(last_media_index>=0),
 updated_at INTEGER NOT NULL,
 FOREIGN KEY(candidate_id) REFERENCES discovery_posts(candidate_id)
);
CREATE TABLE IF NOT EXISTS library_session(
 key TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL,
 media_index INTEGER NOT NULL DEFAULT 0,
 updated_at INTEGER NOT NULL
);
"""

def _safe_media(value):
    if not isinstance(value,dict) or set(value) != {"url","type"}:
        return None
    url=value["url"]
    if not isinstance(url,str) or len(url)>500 or value["type"] not in ("image","video"):
        return None
    try:
        parsed=urlsplit(url)
        valid=(parsed.scheme=="https" and not parsed.username and not parsed.password
               and parsed.port is None and bool(SAFE_MEDIA_HOST.fullmatch(parsed.hostname or "")))
    except ValueError:
        return None
    if not valid:
        return None
    return {"url":url,"type":value["type"]}

class Library:
    def __init__(self, discovery_store):
        self.store=discovery_store

    def _open(self):
        db=self.store._open()
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            return db
        except Exception:
            db.close()
            raise

    @staticmethod
    def _id(candidate_id):
        if not isinstance(candidate_id,str) or IDENT.fullmatch(candidate_id) is None:
            raise ValueError("library_candidate_id_invalid")
        return candidate_id

    def list(self,filter_name="all",offset=0,limit=50):
        if filter_name not in ("all","unread","favorite","watch_later","seen","unavailable"):
            raise ValueError("library_filter_invalid")
        if type(offset) is not int or offset<0 or offset>100000:
            raise ValueError("library_offset_invalid")
        if type(limit) is not int or not 1<=limit<=100:
            raise ValueError("library_limit_invalid")
        clause={
            "all":"p.state='visible' AND COALESCE(b.blocked,0)=0",
            "unread":"p.state='visible' AND COALESCE(b.blocked,0)=0 AND COALESCE(s.seen,0)=0",
            "favorite":"p.state='visible' AND COALESCE(b.blocked,0)=0 AND COALESCE(s.favorite,0)=1",
            "watch_later":"p.state='visible' AND COALESCE(b.blocked,0)=0 AND COALESCE(s.watch_later,0)=1",
            "seen":"p.state='visible' AND COALESCE(b.blocked,0)=0 AND COALESCE(s.seen,0)=1",
            "unavailable":"(p.state='unavailable' OR b.blocked=1)"
        }[filter_name]
        select="SELECT p.candidate_id,p.blog,p.post_id,p.title,p.url,p.post_type,p.posted_at,p.media_json,p.thumbnail_url,p.state,COALESCE(s.seen,0),COALESCE(s.favorite,0),COALESCE(s.watch_later,0),COALESCE(s.last_media_index,0)"
        join=" FROM discovery_posts p LEFT JOIN discovery_blogs b ON b.name=p.blog LEFT JOIN library_state s ON s.candidate_id=p.candidate_id"
        where=" WHERE "+clause
        with closing(self._open()) as db:
            count=db.execute("SELECT COUNT(*)"+join+where).fetchone()[0]
            rows=db.execute(select+join+where+" ORDER BY p.last_seen DESC,p.candidate_id ASC LIMIT ? OFFSET ?",
                            (limit,offset)).fetchall()
            session=db.execute("SELECT candidate_id,media_index FROM library_session WHERE key='last'").fetchone()
        records=[]
        for candidate,blog,post_id,title,url,kind,date,media_json,thumbnail,state,seen,fav,later,index in rows:
            try:
                raw=json.loads(media_json)
            except (TypeError,ValueError):
                raw=[]
            media=[item for item in (_safe_media(x) for x in raw[:50]) if item] if isinstance(raw,list) else []
            records.append({"candidate_id":candidate,"blog":blog,"post_id":post_id,
                "title":title,"url":url,"type":kind,"date":date,"media":media,
                "thumbnail_url":thumbnail,"state":state,"seen":bool(seen),
                "favorite":bool(fav),"watch_later":bool(later),
                "last_media_index":min(index,max(0,len(media)-1))})
        return {"verified":True,"items":records,"total":count,"offset":offset,"limit":limit,
                "filter":filter_name,"last_position":{"candidate_id":session[0],"media_index":session[1]} if session else None,
                "resume_item":self.resume_item() if session else None}

    def resume_item(self):
        """Resolve the previously viewed candidate by stable ID, independent of pagination."""
        with closing(self._open()) as db:
            session=db.execute("SELECT candidate_id,media_index FROM library_session "
                               "WHERE key='last'").fetchone()
            if not session:
                return None
            row=db.execute(
                "SELECT p.candidate_id,p.blog,p.post_id,p.title,p.url,p.post_type,p.posted_at,"
                "p.media_json,p.thumbnail_url,p.state,COALESCE(s.seen,0),"
                "COALESCE(s.favorite,0),COALESCE(s.watch_later,0) "
                "FROM discovery_posts p LEFT JOIN discovery_blogs b ON b.name=p.blog "
                "LEFT JOIN library_state s ON s.candidate_id=p.candidate_id "
                "WHERE p.candidate_id=? AND p.state='visible' AND COALESCE(b.blocked,0)=0",
                (session[0],)).fetchone()
        if row is None:
            return None
        ident,blog,post_id,title,url,kind,date,raw,thumbnail,state,seen,favorite,later=row
        try:
            source=json.loads(raw)
        except (TypeError,ValueError):
            source=[]
        media=[item for item in (_safe_media(x) for x in source[:50]) if item] if isinstance(source,list) else []
        if not media:
            return None
        index=min(session[1],len(media)-1)
        return {"candidate_id":ident,"blog":blog,"post_id":post_id,
                "title":title,"url":url,"type":kind,"date":date,
                "media":media,"thumbnail_url":thumbnail,"state":state,
                "seen":bool(seen),"favorite":bool(favorite),"watch_later":bool(later),
                "last_media_index":index}

    def update(self,candidate_ids,field,value):
        if field not in ("seen","favorite","watch_later"):
            raise ValueError("library_field_invalid")
        if type(value) is not bool or not isinstance(candidate_ids,list) or not 1<=len(candidate_ids)<=100:
            raise ValueError("library_update_invalid")
        ids=[self._id(x) for x in candidate_ids]
        if len(set(ids))!=len(ids):
            raise ValueError("library_duplicate_ids")
        now=int(time.time())
        with closing(self._open()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                rows=db.execute(
                    "SELECT p.candidate_id FROM discovery_posts p LEFT JOIN discovery_blogs b ON b.name=p.blog"
                    " WHERE p.candidate_id IN ("+",".join("?" for _ in ids)+") AND p.state='visible' AND COALESCE(b.blocked,0)=0",ids).fetchall()
                if len(rows)!=len(ids):
                    raise ValueError("library_candidate_unavailable")
                for ident in ids:
                    db.execute("INSERT INTO library_state(candidate_id,updated_at) VALUES(?,?) "
                               "ON CONFLICT(candidate_id) DO NOTHING",(ident,now))
                    db.execute("UPDATE library_state SET "+field+"=?,updated_at=? WHERE candidate_id=?",
                               (int(value),now,ident))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {"verified":True,"updated":len(ids),"field":field,"value":value}

    def progress(self,candidate_id,media_index):
        self._id(candidate_id)
        if type(media_index) is not int or media_index<0 or media_index>49:
            raise ValueError("library_media_index_invalid")
        now=int(time.time())
        with closing(self._open()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row=db.execute("SELECT p.state,p.media_json,COALESCE(b.blocked,0) "
                               "FROM discovery_posts p LEFT JOIN discovery_blogs b ON b.name=p.blog "
                               "WHERE p.candidate_id=?",(candidate_id,)).fetchone()
                if not row or row[0]!="visible" or row[2]:
                    raise ValueError("library_candidate_unavailable")
                media=json.loads(row[1])
                if not isinstance(media,list) or not media or media_index>=len(media):
                    raise ValueError("library_media_index_invalid")
                db.execute("INSERT INTO library_state(candidate_id,seen,last_media_index,updated_at)"
                           " VALUES(?,1,?,?) ON CONFLICT(candidate_id) DO UPDATE SET seen=1,"
                           " last_media_index=excluded.last_media_index,updated_at=excluded.updated_at",
                           (candidate_id,media_index,now))
                db.execute("INSERT INTO library_session(key,candidate_id,media_index,updated_at)"
                           " VALUES('last',?,?,?) ON CONFLICT(key) DO UPDATE SET"
                           " candidate_id=excluded.candidate_id,media_index=excluded.media_index,"
                           " updated_at=excluded.updated_at",(candidate_id,media_index,now))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return {"verified":True,"candidate_id":candidate_id,"media_index":media_index}
