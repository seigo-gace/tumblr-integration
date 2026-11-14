"""Owner-private campaign contest lifecycle on the existing core SQLite ledger.

This module does not send Telegram messages, request Tumblr API calls, or store
shipping addresses. The caller must inject *trusted* eligibility checks. A draw
is committed once; its seed and ranking never change on repeated requests.
"""
import hashlib
import json
import secrets
import time
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_contests(
 campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),
 prize TEXT NOT NULL,
 deadline INTEGER NOT NULL,
 winner_count INTEGER NOT NULL CHECK(winner_count>0),
 claim_seconds INTEGER NOT NULL CHECK(claim_seconds>0),
 seed TEXT NOT NULL,
 seed_commitment TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','drawn','closed')),
 drawn_at INTEGER
);
CREATE TABLE IF NOT EXISTS campaign_entries(
 campaign_id TEXT NOT NULL REFERENCES campaign_contests(campaign_id),
 applicant_id TEXT NOT NULL,
 entered_at INTEGER NOT NULL,
 rank INTEGER,
 status TEXT NOT NULL CHECK(status IN
 ('entered','winner','waitlisted','claimed','expired')),
 claim_deadline INTEGER,
 PRIMARY KEY(campaign_id,applicant_id),
 UNIQUE(campaign_id,rank)
);
CREATE TABLE IF NOT EXISTS campaign_outcomes(
 campaign_id TEXT NOT NULL,
 applicant_id TEXT NOT NULL,
 state TEXT NOT NULL,
 event_id TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL,
 PRIMARY KEY(campaign_id,applicant_id,state),
 FOREIGN KEY(campaign_id,applicant_id)
 REFERENCES campaign_entries(campaign_id,applicant_id)
);
CREATE TABLE IF NOT EXISTS campaign_notifications(
 event_id TEXT PRIMARY KEY REFERENCES campaign_outcomes(event_id),
 state TEXT NOT NULL CHECK(state IN
 ('pending','sending','verified','needs_reconcile')),
 message_ref TEXT,
 updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class CampaignDrawResult:
    campaign_id: str
    commitment: str
    winner_count: int
    participants: int
    winners: tuple[str, ...]


class CampaignContest:
    def __init__(self, db, *, clock=None, eligibility=None):
        self.db = db
        self.clock = clock or time.time
        self.eligibility = eligibility
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript(SCHEMA)

    @staticmethod
    def _ident(value):
        if (not isinstance(value, str) or not 1 <= len(value) <= 100
                or any(ord(c) < 33 for c in value)):
            raise ValueError("campaign_identifier_invalid")
        return value

    def create(self, campaign_id, prize, *, deadline, winners, claim_seconds=86400):
        self._ident(campaign_id)
        if not isinstance(prize, str) or not 1 <= len(prize.strip()) <= 200:
            raise ValueError("campaign_prize_invalid")
        now = int(self.clock())
        if (type(deadline) is not int or deadline <= now or
                type(winners) is not int or not 1 <= winners <= 10000 or
                type(claim_seconds) is not int or not 60 <= claim_seconds <= 2592000):
            raise ValueError("campaign_terms_invalid")
        seed = secrets.token_hex(32)
        commitment = hashlib.sha256(bytes.fromhex(seed)).hexdigest()
        with self.db:
            self.db.execute("INSERT INTO campaigns(id,name,active) VALUES(?,?,1)",
                            (campaign_id,prize.strip()))
            self.db.execute(
                "INSERT INTO campaign_contests VALUES(?,?,?,?,?,?,?,?,?)",
                (campaign_id,prize.strip(),deadline,winners,claim_seconds,
                 seed,commitment,"open",None))
        return {"campaign_id":campaign_id,"seed_commitment":commitment,
                "deadline":deadline,"winners":winners}

    def change_deadline(self, campaign_id, deadline):
        """Owner can amend an open campaign, not rewrite a completed draw."""
        self._ident(campaign_id)
        now=int(self.clock())
        if type(deadline) is not int or deadline <= now:
            raise ValueError("campaign_deadline_invalid")
        with self.db:
            changed=self.db.execute(
                "UPDATE campaign_contests SET deadline=? "
                "WHERE campaign_id=? AND state='open' AND deadline>?",
                (deadline,campaign_id,now)).rowcount
        if changed!=1:
            raise ValueError("campaign_deadline_locked")
        return {"campaign_id":campaign_id,"deadline":deadline}

    def entries(self, campaign_id):
        self._ident(campaign_id)
        return [{"applicant_id":r[0],"status":r[1],
                 "entered_at":r[2],"rank":r[3],
                 "claim_deadline":r[4]}
                for r in self.db.execute(
                    "SELECT applicant_id,status,entered_at,rank,claim_deadline "
                    "FROM campaign_entries WHERE campaign_id=? "
                    "ORDER BY entered_at,applicant_id",(campaign_id,))]

    def enter(self, campaign_id, applicant_id):
        """Only trusted eligibility adapters may accept entries."""
        self._ident(campaign_id)
        self._ident(applicant_id)
        if self.eligibility is None:
            raise PermissionError("campaign_eligibility_not_connected")
        # No untrusted boolean parameter is accepted from the web client.
        if self.eligibility(campaign_id, applicant_id) is not True:
            raise PermissionError("campaign_eligibility_not_verified")
        now = int(self.clock())
        with self.db:
            row = self.db.execute(
                "SELECT state,deadline FROM campaign_contests WHERE campaign_id=?",
                (campaign_id,)).fetchone()
            if not row or row[0] != "open" or now >= row[1]:
                raise ValueError("campaign_not_accepting_entries")
            created = self.db.execute(
                "INSERT OR IGNORE INTO campaign_entries"
                "(campaign_id,applicant_id,entered_at,status) VALUES(?,?,?,'entered')",
                (campaign_id,applicant_id,now)).rowcount
        return "entered" if created else "already_entered"

    @staticmethod
    def _order(seed, applicant_id):
        # The private seed was committed at campaign creation, so the owner
        # cannot adjust the winner order at draw time.
        return hashlib.sha256(bytes.fromhex(seed)+applicant_id.encode("utf-8")).digest()

    def draw(self, campaign_id):
        self._ident(campaign_id)
        now = int(self.clock())
        with self.db:
            row = self.db.execute(
                "SELECT deadline,winner_count,claim_seconds,seed,seed_commitment,state "
                "FROM campaign_contests WHERE campaign_id=?", (campaign_id,)).fetchone()
            if not row:
                raise ValueError("campaign_missing")
            deadline,count,claim_seconds,seed,commitment,state = row
            if state == "closed":
                raise ValueError("campaign_closed")
            if state == "open":
                if now < deadline:
                    raise ValueError("campaign_draw_too_early")
                applicants = [r[0] for r in self.db.execute(
                    "SELECT applicant_id FROM campaign_entries WHERE campaign_id=?",
                    (campaign_id,))]
                ranked = sorted(applicants,key=lambda x:(self._order(seed,x),x))
                for rank,applicant in enumerate(ranked):
                    winner = rank < count
                    self.db.execute(
                        "UPDATE campaign_entries SET rank=?,status=?,claim_deadline=? "
                        "WHERE campaign_id=? AND applicant_id=?",
                        (rank,"winner" if winner else "waitlisted",
                         now+claim_seconds if winner else None,campaign_id,applicant))
                    if winner:
                        self._outcome(campaign_id,applicant,"winner",now)
                self.db.execute(
                    "UPDATE campaign_contests SET state='drawn',drawn_at=? WHERE campaign_id=?",
                    (now,campaign_id))
            rows = self.db.execute(
                "SELECT applicant_id,rank FROM campaign_entries "
                "WHERE campaign_id=? ORDER BY rank",(campaign_id,)).fetchall()
        # The original random draw never changes after claims, expiry or
        # waitlist promotions. Promotions are tracked as separate outcomes.
        winners = tuple(item for item,rank in rows if rank < count)
        return CampaignDrawResult(campaign_id,commitment,count,len(rows),winners)

    def _outcome(self,campaign_id,applicant_id,state,now):
        event_id = hashlib.sha256(
            json.dumps([campaign_id,applicant_id,state],ensure_ascii=False).encode()
        ).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO campaign_outcomes VALUES(?,?,?,?,?)",
            (campaign_id,applicant_id,state,event_id,now))
        self.db.execute(
            "INSERT OR IGNORE INTO campaign_notifications"
            "(event_id,state,updated_at) VALUES(?,'pending',?)",
            (event_id,now))

    def claim(self,campaign_id,applicant_id):
        self._ident(campaign_id)
        self._ident(applicant_id)
        now=int(self.clock())
        with self.db:
            row=self.db.execute(
                "SELECT status,claim_deadline FROM campaign_entries "
                "WHERE campaign_id=? AND applicant_id=?",
                (campaign_id,applicant_id)).fetchone()
            if not row:
                raise ValueError("campaign_entry_missing")
            if row[0]=="claimed":
                return "already_claimed"
            if row[0]!="winner" or row[1] is None or now >= row[1]:
                raise ValueError("campaign_claim_not_available")
            self.db.execute(
                "UPDATE campaign_entries SET status='claimed' "
                "WHERE campaign_id=? AND applicant_id=?",
                (campaign_id,applicant_id))
            self._outcome(campaign_id,applicant_id,"claimed",now)
        return "claimed"

    def promote_expired(self,campaign_id):
        """Expire unclaimed winners and promote the next ranked entry once."""
        self._ident(campaign_id)
        now=int(self.clock())
        with self.db:
            contest=self.db.execute(
                "SELECT state,claim_seconds FROM campaign_contests WHERE campaign_id=?",
                (campaign_id,)).fetchone()
            if not contest or contest[0]!="drawn":
                raise ValueError("campaign_not_drawn")
            expired=self.db.execute(
                "SELECT applicant_id FROM campaign_entries WHERE campaign_id=? "
                "AND status='winner' AND claim_deadline<=? ORDER BY rank",
                (campaign_id,now)).fetchall()
            promoted=[]
            for (applicant,) in expired:
                self.db.execute(
                    "UPDATE campaign_entries SET status='expired' "
                    "WHERE campaign_id=? AND applicant_id=?",
                    (campaign_id,applicant))
                self._outcome(campaign_id,applicant,"expired",now)
                candidate=self.db.execute(
                    "SELECT applicant_id FROM campaign_entries "
                    "WHERE campaign_id=? AND status='waitlisted' "
                    "ORDER BY rank LIMIT 1",(campaign_id,)).fetchone()
                if candidate:
                    next_id=candidate[0]
                    self.db.execute(
                        "UPDATE campaign_entries SET status='winner',claim_deadline=? "
                        "WHERE campaign_id=? AND applicant_id=?",
                        (now+contest[1],campaign_id,next_id))
                    self._outcome(campaign_id,next_id,"winner",now)
                    promoted.append(next_id)
        return promoted

    def close(self,campaign_id):
        self._ident(campaign_id)
        with self.db:
            row=self.db.execute(
                "UPDATE campaign_contests SET state='closed' "
                "WHERE campaign_id=? AND state='drawn' RETURNING campaign_id",
                (campaign_id,)).fetchone()
        if row:
            return "closed"
        state=self.db.execute(
            "SELECT state FROM campaign_contests WHERE campaign_id=?",
            (campaign_id,)).fetchone()
        if state and state[0]=="closed":
            return "already_closed"
        raise ValueError("campaign_not_drawn")

    def pending_notifications(self,campaign_id):
        """Read-only delivery work for existing MPB; never invent a Bot DM."""
        self._ident(campaign_id)
        return [{"event_id":r[0],"applicant_id":r[1],"outcome":r[2]}
                for r in self.db.execute(
                    "SELECT n.event_id,o.applicant_id,o.state "
                    "FROM campaign_notifications n JOIN campaign_outcomes o "
                    "ON o.event_id=n.event_id "
                    "WHERE o.campaign_id=? AND n.state='pending' "
                    "ORDER BY o.created_at,n.event_id",(campaign_id,))]

    def dispatch_notification(self, event_id, messenger):
        """Existing owner transport must deliver + readback. No blind replay."""
        if not isinstance(event_id,str) or len(event_id)!=64:
            raise ValueError("campaign_event_invalid")
        if messenger is None:
            raise ValueError("campaign_messenger_not_connected")
        now=int(self.clock())
        with self.db:
            selected=self.db.execute(
                "SELECT o.campaign_id,o.applicant_id,o.state "
                "FROM campaign_notifications n JOIN campaign_outcomes o "
                "ON o.event_id=n.event_id WHERE n.event_id=? AND n.state='pending'",
                (event_id,)).fetchone()
            if not selected:
                raise ValueError("campaign_notification_not_pending")
            changed=self.db.execute(
                "UPDATE campaign_notifications SET state='sending',updated_at=? "
                "WHERE event_id=? AND state='pending'",(now,event_id)).rowcount
            if changed!=1:
                raise ValueError("campaign_notification_not_pending")
        campaign_id,applicant_id,outcome=selected
        reference=None
        verified=False
        try:
            response=messenger.publish(event_id,campaign_id,applicant_id,outcome)
            reference=response.get("message_ref") if isinstance(response,dict) else None
            if isinstance(reference,str) and reference:
                verified=messenger.readback(event_id,reference) is True
        except Exception:
            verified=False
        with self.db:
            self.db.execute(
                "UPDATE campaign_notifications SET state=?,message_ref=?,updated_at=? "
                "WHERE event_id=? AND state='sending'",
                ("verified" if verified else "needs_reconcile",reference,now,event_id))
        return {"event_id":event_id,
                "state":"verified" if verified else "needs_reconcile",
                "message_ref":reference}

    def list_campaigns(self):
        """Owner-only management data; never expose the private draw seed."""
        rows = self.db.execute(
            "SELECT c.campaign_id,c.prize,c.deadline,c.winner_count,"
            "c.claim_seconds,c.seed_commitment,c.state,"
            "(SELECT COUNT(*) FROM campaign_entries e "
            "WHERE e.campaign_id=c.campaign_id) "
            "FROM campaign_contests c ORDER BY c.deadline,c.campaign_id"
        ).fetchall()
        return [
            {"campaign_id":r[0],"prize":r[1],"deadline":r[2],
             "winner_count":r[3],"claim_seconds":r[4],
             "commitment":r[5],"state":r[6],"entries":r[7]}
            for r in rows
        ]

    def outcomes(self,campaign_id):
        self._ident(campaign_id)
        return [dict(applicant_id=r[0],state=r[1],event_id=r[2])
                for r in self.db.execute(
                    "SELECT applicant_id,state,event_id FROM campaign_outcomes "
                    "WHERE campaign_id=? ORDER BY created_at,event_id",
                    (campaign_id,))]
