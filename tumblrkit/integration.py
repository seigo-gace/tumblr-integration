"""Purpose-specific Tumblr↔Telegram integration; external adapters remain owned elsewhere."""
from dataclasses import dataclass
from typing import Protocol
import secrets
import hashlib
import json
from datetime import datetime, timezone

from tumblrkit.core import record_event, confirm_membership

class LinkStore(Protocol):
    def create(self, campaign_id: str, source_post_id: str) -> str: ...
    def mark(self, event_id: str, event_type: str, link_id: str) -> bool: ...

class TelegramGate(Protocol):
    def verify_member(self, group_id: str, telegram_user_id: str) -> bool: ...

class TelegramPublisher(Protocol):
    # The existing MPB owner must resolve origin + actor + destination and
    # authorize the operation before Tumblr can request any delivery.
    def authorize(self, destination: str, operation_key: str) -> bool: ...
    def publish(self, destination: str, text: str, operation_key: str) -> dict: ...
    def readback(self, destination: str, message_id: str) -> bool: ...

class CoreLinkStore:
    """Campaign-owned durable link and click adapter over the existing SQLite.

    The returned ID is an opaque reference, not a join URL or membership proof.
    A real public link and GATE authentication remain the owning adapters' job.
    """
    def __init__(self, db):
        self.db=db

    def create(self, campaign_id: str, source_post_id: str) -> str:
        if not all(isinstance(v,str) and v for v in (campaign_id,source_post_id)):
            raise ValueError("campaign/post ID required")
        with self.db:
            campaign=self.db.execute(
                "SELECT active FROM campaigns WHERE id=?",(campaign_id,)).fetchone()
            if not campaign or not campaign[0]:
                raise ValueError("campaign inactive or missing")
            existing=self.db.execute(
                "SELECT link_id FROM campaign_links WHERE campaign_id=? AND source_post_id=?",
                (campaign_id,source_post_id)).fetchone()
            if existing:
                return existing[0]
            fresh=secrets.token_urlsafe(18)
            self.db.execute(
                "INSERT INTO campaign_links(link_id,campaign_id,source_post_id) "
                "VALUES(?,?,?) ON CONFLICT(campaign_id,source_post_id) DO NOTHING",
                (fresh,campaign_id,source_post_id))
            persisted=self.db.execute(
                "SELECT link_id FROM campaign_links WHERE campaign_id=? AND source_post_id=?",
                (campaign_id,source_post_id)).fetchone()
            return persisted[0]

    def mark(self, event_id: str, event_type: str, link_id: str) -> bool:
        if event_type!="entry" or not all(
                isinstance(value,str) and value for value in (event_id,link_id)):
            raise ValueError("campaign_entry_invalid")
        with self.db:
            exists=self.db.execute(
                "SELECT 1 FROM campaign_links WHERE link_id=?",(link_id,)).fetchone()
            if not exists:
                raise ValueError("campaign_link_missing")
            # record_event checks a replayed ID against its original source.
            # A second click cannot silently point to a different campaign.
            return record_event(self.db,event_id,event_type,link_id)


@dataclass(frozen=True)
class MembershipAttribution:
    status: str
    new_rewards: tuple[str, ...] = ()


@dataclass(frozen=True)
class HandoffResult:
    status: str
    next_actions: tuple[str, ...]
    reference: str | None = None

def _has_file_backed_ledger(db) -> bool:
    """Reject volatile receipts before any externally visible MPB operation.

    A memory-backed SQLite ledger loses uncertain delivery state on restart.
    Only a connected, file-backed main database can guard an MPB request.
    The owner still controls file permissions and persistent mount lifecycle.
    """
    try:
        for _, name, path in db.execute("PRAGMA database_list").fetchall():
            if name == "main":
                return isinstance(path, str) and bool(path.strip())
    except Exception:
        return False
    return False


class CampaignHandoff:
    def __init__(self, links: LinkStore, gate: TelegramGate, publisher: TelegramPublisher,
                 *, campaign_group=None, delivery_db=None):
        self.links, self.gate, self.publisher = links, gate, publisher
        # Trusted owner-provided campaign->group binding, not an untrusted
        # group_id from a browser or a public referral URL.
        self.campaign_group = campaign_group
        # Owner-private persistent SQLite connection; never a browser cache.
        # No MPB effect is admitted without a durable operation receipt.
        self.delivery_db = delivery_db

    def referral_entry(self, campaign_id: str, tumblr_post_id: str) -> HandoffResult:
        if not campaign_id or not tumblr_post_id:
            raise ValueError("campaign/post ID required")
        link_id = self.links.create(campaign_id, tumblr_post_id)
        return HandoffResult("link_prepared", ("preview_share", "inspect_clicks"), link_id)

    def track_entry(self, event_id: str, link_id: str) -> HandoffResult:
        if not event_id or not link_id:
            raise ValueError("event/link ID required")
        fresh = self.links.mark(event_id, "entry", link_id)
        return HandoffResult("recorded" if fresh else "already_recorded", ("check_membership",))

    def check_membership(self, group_id: str, telegram_user_id: str) -> HandoffResult:
        if not group_id or not telegram_user_id:
            raise ValueError("group/user ID required")
        # The GATE adapter alone supplies a trusted boolean readback.
        # Never accept integer 1 or nonempty strings as proof of membership.
        try:
            verified = self.gate.verify_member(group_id, telegram_user_id) is True
        except Exception:
            # An unavailable GATE is not evidence of membership.
            verified = False
        if verified:
            return HandoffResult("member_verified", ("evaluate_campaign_eligibility",))
        return HandoffResult("not_verified", ("show_join_instructions", "retry_verification"))

    def attribute_membership(self, db, campaign_id: str, group_id: str,
                             telegram_user_id: str, referral_code=None) -> MembershipAttribution:
        """One trusted GATE readback -> one local membership/tier attribution.

        No public API, cross-service credential, or membership guess is made.
        The caller must inject the trusted group assignment from the GATE
        owner; absent assignment fails closed before any membership query.
        """
        if not all(isinstance(v,str) and v for v in
                   (campaign_id,group_id,telegram_user_id)):
            raise ValueError("campaign_membership_target_invalid")
        if self.campaign_group is None:
            raise PermissionError("campaign_group_binding_required")
        owned_group=self.campaign_group(campaign_id)
        if not isinstance(owned_group,str) or not owned_group or owned_group!=group_id:
            raise PermissionError("campaign_group_mismatch")
        try:
            verified = self.gate.verify_member(group_id,telegram_user_id) is True
        except Exception:
            verified = False
        if not verified:
            return MembershipAttribution("not_verified")
        grants=confirm_membership(db,campaign_id,telegram_user_id,
                                  gate_verified=True,referral_code=referral_code)
        return MembershipAttribution("member_verified",tuple(grants))

    def reconcile_announcement(self, destination: str, text: str,
                               operation_key: str) -> HandoffResult:
        """Read-only MPB recovery by stable operation key; never repost."""
        if not all(isinstance(v,str) and v for v in (destination,text,operation_key)):
            raise ValueError("destination/text/key required")
        if not _has_file_backed_ledger(self.delivery_db):
            return HandoffResult("not_connected",("configure_durable_delivery",))
        authorize=getattr(self.publisher,"authorize",None)
        try:
            authorized = callable(authorize) and authorize(destination,operation_key) is True
        except Exception:
            authorized = False
        if not authorized:
            return HandoffResult("not_connected", ("resolve_mpb_destination",))
        db=self.delivery_db
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                          "AND name='announcement_receipts'").fetchone()
        if not exists:
            return HandoffResult("unverified",("reconcile_delivery",))
        row=db.execute("SELECT payload_digest,destination,state,message_id "
                       "FROM announcement_receipts WHERE operation_key=?",
                       (operation_key,)).fetchone()
        if row is None:
            return HandoffResult("unverified",("reconcile_delivery",))
        fingerprint=hashlib.sha256(json.dumps(
            [destination,text],ensure_ascii=False,separators=(",",":")
        ).encode("utf-8")).hexdigest()
        if row[0]!=fingerprint or row[1]!=destination:
            raise ValueError("announcement_operation_conflict")
        state,message_id=row[2],row[3]
        if state=="verified":
            if not isinstance(message_id,str) or not message_id:
                raise ValueError("announcement_receipt_invalid")
            return HandoffResult("published",("inspect_post","measure_referrals"),message_id)
        if state=="reserved":
            if message_id is not None:
                raise ValueError("announcement_receipt_invalid")
            # This is a separately owned, read-only MPB operation lookup.
            # No query endpoint is invented here; the injected trusted MPB
            # adapter must implement it before recovery can be used live.
            lookup=getattr(self.publisher,"find_by_operation_key",None)
            if not callable(lookup):
                return HandoffResult("unverified",("reconcile_delivery",))
            try:
                found=lookup(destination,operation_key)
            except Exception:
                return HandoffResult("unverified",("reconcile_delivery",))
            message_id=found.get("message_id") if isinstance(found,dict) else None
            if not isinstance(message_id,str) or not message_id:
                return HandoffResult("unverified",("reconcile_delivery",))
            with db:
                db.execute("UPDATE announcement_receipts "
                           "SET state='sent',message_id=?,updated_at=? "
                           "WHERE operation_key=? AND state='reserved'",
                           (message_id,datetime.now(timezone.utc).isoformat(),operation_key))
        elif state!="sent" or not isinstance(message_id,str) or not message_id:
            raise ValueError("announcement_receipt_invalid")
        try:
            matched=self.publisher.readback(destination,message_id) is True
        except Exception:
            matched=False
        if not matched:
            return HandoffResult("unverified",("reconcile_delivery",),message_id)
        with db:
            db.execute("UPDATE announcement_receipts "
                       "SET state='verified',updated_at=? "
                       "WHERE operation_key=? AND state='sent'",
                       (datetime.now(timezone.utc).isoformat(),operation_key))
        return HandoffResult("published",("inspect_post","measure_referrals"),message_id)

    def publish_announcement(self, destination: str, text: str, operation_key: str, *, confirmed: bool = False) -> HandoffResult:
        if not all(isinstance(v,str) and v for v in (destination,text,operation_key)):
            raise ValueError("destination/text/key required")
        if confirmed is not True:
            return HandoffResult("confirmation_required", ("confirm_publish","edit_draft"))
        if not _has_file_backed_ledger(self.delivery_db):
            # Never call MPB with an in-memory or unavailable receipt ledger.
            return HandoffResult("not_connected", ("configure_durable_delivery",))
        # Never let a caller-chosen group/topic travel straight to MPB. The
        # injected MPB-owned adapter must independently resolve origin and
        # actor permissions; a truthy result is not sufficient evidence.
        authorize=getattr(self.publisher,"authorize",None)
        try:
            authorized = callable(authorize) and authorize(destination,operation_key) is True
        except Exception:
            authorized = False
        if not authorized:
            return HandoffResult("not_connected", ("resolve_mpb_destination",))
        fingerprint=hashlib.sha256(json.dumps(
            [destination,text],ensure_ascii=False,separators=(",",":")
        ).encode("utf-8")).hexdigest()
        db=self.delivery_db
        with db:
            # Uses the same owner-private campaign DB; no second persistence.
            db.execute("""CREATE TABLE IF NOT EXISTS announcement_receipts (
              operation_key TEXT PRIMARY KEY,
              payload_digest TEXT NOT NULL,
              destination TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('reserved','sent','verified')),
              message_id TEXT,
              updated_at TEXT NOT NULL
            )""")
            created=db.execute("INSERT OR IGNORE INTO announcement_receipts "
                       "(operation_key,payload_digest,destination,state,updated_at) "
                       "VALUES(?,?,?,'reserved',?)",
                       (operation_key,fingerprint,destination,
                        datetime.now(timezone.utc).isoformat())).rowcount==1
            row=db.execute("SELECT payload_digest,destination,state,message_id "
                           "FROM announcement_receipts WHERE operation_key=?",
                           (operation_key,)).fetchone()
            if row[0]!=fingerprint or row[1]!=destination:
                raise ValueError("announcement_operation_conflict")
        state,message_id=row[2],row[3]
        if state in ("sent","verified") and (not isinstance(message_id,str) or not message_id):
            raise ValueError("announcement_receipt_invalid")
        if state=="verified":
            return HandoffResult("published", ("inspect_post","measure_referrals"),message_id)
        if state=="reserved" and row[3] is not None:
            raise ValueError("announcement_receipt_invalid")
        # Only a newly reserved operation may call MPB publish. Existing
        # reserved but uncertain operation keys must never be resubmitted.
        # Determine this from INSERT rowcount under the transaction.
        if state=="reserved" and not created:
            return HandoffResult("unverified", ("reconcile_delivery",))
        if state=="reserved":
            try:
                receipt=self.publisher.publish(destination,text,operation_key)
            except Exception:
                return HandoffResult("unverified", ("reconcile_delivery",))
            message_id=receipt.get("message_id") if isinstance(receipt,dict) else None
            if not isinstance(message_id,str) or not message_id:
                return HandoffResult("unverified", ("reconcile_delivery",))
            with db:
                db.execute("UPDATE announcement_receipts "
                           "SET state='sent',message_id=?,updated_at=? "
                           "WHERE operation_key=? AND state='reserved'",
                           (message_id,datetime.now(timezone.utc).isoformat(),operation_key))
        # For a sent-but-unverified request, only read back the same known ID.
        try:
            matched = self.publisher.readback(destination,message_id) is True
        except Exception:
            # The MPB operation may have succeeded; retain the durable sent
            # receipt and reconcile later instead of losing the result.
            matched = False
        if not matched:
            return HandoffResult("unverified", ("reconcile_delivery",),message_id)
        with db:
            db.execute("UPDATE announcement_receipts "
                       "SET state='verified',updated_at=? WHERE operation_key=? AND state='sent'",
                       (datetime.now(timezone.utc).isoformat(),operation_key))
        return HandoffResult("published", ("inspect_post","measure_referrals"),message_id)
