"""Persistent, fail-closed limits for deliberate Tumblr read operations.

Local safety thresholds are deliberately below provider quotas; not a ban guarantee.
The same SQLite file must be shared by all read adapter processes on Gserver.
"""
from pathlib import Path
from contextlib import closing
import os
import stat
import sqlite3
import time

class TumblrReadBudget:
    # One intentional read fetches owner identity plus posts (2 API calls).
    MAX_HOURLY_CALLS = 12
    MAX_DAILY_CALLS = 30
    MIN_OPERATION_GAP = 60
    LIMIT_PAUSE_SECONDS = 24 * 60 * 60

    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock or time.time

    def _open(self):
        folder = self.path.parent
        if (folder.is_symlink() or not folder.is_dir() or
                folder.stat().st_uid != os.getuid() or folder.stat().st_mode & 0o077):
            raise ValueError("tumblr_budget_directory_unsafe")
        if self.path.is_symlink():
            raise ValueError("tumblr_budget_symlink")
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        info = self.path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ValueError("tumblr_budget_file_unsafe")
        return sqlite3.connect(self.path, timeout=3, isolation_level=None)

    def reserve(self, calls=2):
        if calls not in (1, 2):
            raise ValueError("tumblr_read_cost_mismatch")
        now = int(self.clock())
        hour, day = now // 3600, now // 86400
        try:
            with closing(self._open()) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS tumblr_api_read_budget "
                           "(period TEXT NOT NULL, bucket INTEGER NOT NULL, calls INTEGER NOT NULL, "
                           "PRIMARY KEY(period,bucket))")
                db.execute("CREATE TABLE IF NOT EXISTS tumblr_api_read_control "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), last_attempt INTEGER NOT NULL, "
                           "blocked_until INTEGER NOT NULL)")
                last = db.execute("SELECT last_attempt,blocked_until FROM tumblr_api_read_control WHERE id=1").fetchone()
                if last and (now < last[1] or now-last[0] < self.MIN_OPERATION_GAP):
                    raise ValueError("tumblr_budget_cooldown")
                for period,bucket,max_calls in (("hour",hour,self.MAX_HOURLY_CALLS),
                                                ("day",day,self.MAX_DAILY_CALLS)):
                    row = db.execute("SELECT calls FROM tumblr_api_read_budget WHERE period=? AND bucket=?",
                                     (period,bucket)).fetchone()
                    if (row[0] if row else 0)+calls > max_calls:
                        raise ValueError("tumblr_budget_exhausted")
                for period,bucket in (("hour",hour),("day",day)):
                    db.execute("INSERT INTO tumblr_api_read_budget(period,bucket,calls) VALUES(?,?,?) "
                               "ON CONFLICT(period,bucket) DO UPDATE SET calls=calls+excluded.calls",
                               (period,bucket,calls))
                db.execute("INSERT INTO tumblr_api_read_control(id,last_attempt,blocked_until) VALUES(1,?,0) "
                           "ON CONFLICT(id) DO UPDATE SET last_attempt=excluded.last_attempt", (now,))
                db.execute("COMMIT")
        except (sqlite3.Error, OSError) as exc:
            raise ValueError("tumblr_budget_unavailable") from None

    def pause_after_limit(self):
        now = int(self.clock())
        try:
            with self._open() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("CREATE TABLE IF NOT EXISTS tumblr_api_read_control "
                           "(id INTEGER PRIMARY KEY CHECK(id=1), last_attempt INTEGER NOT NULL, "
                           "blocked_until INTEGER NOT NULL)")
                db.execute("INSERT INTO tumblr_api_read_control(id,last_attempt,blocked_until) VALUES(1,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET blocked_until=MAX(blocked_until,excluded.blocked_until)",
                           (now,now+self.LIMIT_PAUSE_SECONDS))
                db.execute("COMMIT")
        except (sqlite3.Error, OSError):
            raise ValueError("tumblr_budget_unavailable") from None
