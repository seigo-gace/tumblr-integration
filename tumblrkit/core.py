"""Dependency-free, local-only prototype for referral and campaign bookkeeping.

No provider or GATE network calls. gate_verified must be supplied by a trusted
internal integration adapter, not directly from a public request.
"""
from __future__ import annotations
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS referrals (
 campaign_id TEXT NOT NULL, referrer_id TEXT NOT NULL,
 code TEXT NOT NULL UNIQUE,
 PRIMARY KEY(campaign_id, referrer_id),
 FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS memberships (
 campaign_id TEXT NOT NULL, member_id TEXT NOT NULL,
 referrer_id TEXT, verified_at TEXT NOT NULL,
 PRIMARY KEY(campaign_id, member_id),
 FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
);
CREATE TABLE IF NOT EXISTS tiers (
 campaign_id TEXT NOT NULL, threshold INTEGER NOT NULL, reward TEXT NOT NULL,
 PRIMARY KEY(campaign_id, threshold)
);
CREATE TABLE IF NOT EXISTS grants (
 campaign_id TEXT NOT NULL, referrer_id TEXT NOT NULL, threshold INTEGER NOT NULL,
 reward TEXT NOT NULL, awarded_at TEXT NOT NULL,
 PRIMARY KEY(campaign_id, referrer_id, threshold)
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
 post_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, seen INTEGER NOT NULL DEFAULT 0,
 favorite INTEGER NOT NULL DEFAULT 0, watch_later INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
 job_key TEXT PRIMARY KEY, action TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending'
);
"""

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    return db

def create_campaign(db, campaign_id: str, name: str, tiers: dict[int, str]) -> None:
    if not campaign_id or not name:
        raise ValueError("campaign id/name required")
    with db:
        db.execute("INSERT INTO campaigns(id,name) VALUES(?,?)", (campaign_id,name))
        for threshold,reward in tiers.items():
            if threshold < 1 or not reward:
                raise ValueError("invalid reward tier")
            db.execute("INSERT INTO tiers VALUES(?,?,?)", (campaign_id,threshold,reward))

def issue_referral(db, campaign_id: str, referrer_id: str) -> str:
    if not referrer_id:
        raise ValueError("referrer required")
    row = db.execute("SELECT code FROM referrals WHERE campaign_id=? AND referrer_id=?", (campaign_id,referrer_id)).fetchone()
    if row: return row[0]
    code = secrets.token_urlsafe(12)
    with db:
        db.execute("INSERT INTO referrals VALUES(?,?,?)",(campaign_id,referrer_id,code))
    return code

def confirm_membership(db, campaign_id: str, member_id: str, *, gate_verified: bool, referral_code: str | None = None) -> list[str]:
    if not gate_verified:
        raise PermissionError("verified GATE event required")
    if not member_id:
        raise ValueError("member required")
    campaign = db.execute("SELECT active FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
    if not campaign or not campaign[0]:
        raise ValueError("campaign inactive or missing")
    referrer = None
    if referral_code:
        row = db.execute("SELECT referrer_id FROM referrals WHERE campaign_id=? AND code=?", (campaign_id,referral_code)).fetchone()
        if not row: raise ValueError("invalid referral")
        referrer = row[0]
        if referrer == member_id: raise ValueError("self referral")
    with db:
        inserted = db.execute("INSERT OR IGNORE INTO memberships VALUES(?,?,?,?)",
                              (campaign_id,member_id,referrer,utc_now())).rowcount
        if not inserted or referrer is None:
            return []
        count = db.execute("SELECT count(*) FROM memberships WHERE campaign_id=? AND referrer_id=?",(campaign_id,referrer)).fetchone()[0]
        granted = []
        for threshold,reward in db.execute("SELECT threshold,reward FROM tiers WHERE campaign_id=? AND threshold<=?",(campaign_id,count)).fetchall():
            if db.execute("INSERT OR IGNORE INTO grants VALUES(?,?,?,?,?)",
                          (campaign_id,referrer,threshold,reward,utc_now())).rowcount:
                granted.append(reward)
        return granted

def record_event(db, event_id: str, kind: str, source: str) -> bool:
    with db:
        return bool(db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?,?)",(event_id,kind,source,utc_now())).rowcount)

def event_counts(db) -> list[tuple[str,str,int]]:
    return db.execute("SELECT source,kind,count(*) FROM events GROUP BY source,kind ORDER BY source,kind").fetchall()

def upsert_post(db, post_id: str, source_url: str) -> None:
    with db:
        db.execute("INSERT INTO posts(post_id,source_url) VALUES(?,?) ON CONFLICT(post_id) DO UPDATE SET source_url=excluded.source_url",(post_id,source_url))

def mark_post(db, post_id: str, *, seen=None, favorite=None, watch_later=None) -> None:
    values = {"seen":seen,"favorite":favorite,"watch_later":watch_later}
    updates = {k:int(bool(v)) for k,v in values.items() if v is not None}
    if not updates:return
    with db:
        db.execute("UPDATE posts SET "+", ".join(f"{k}=?" for k in updates)+" WHERE post_id=?",(*updates.values(),post_id))

def enqueue(db, key: str, action: str) -> bool:
    with db:
        return bool(db.execute("INSERT OR IGNORE INTO jobs(job_key,action) VALUES(?,?)",(key,action)).rowcount)
