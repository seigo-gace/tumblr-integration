"""Purpose-specific Tumblr↔Telegram integration; external adapters remain owned elsewhere."""
from dataclasses import dataclass
from typing import Protocol
import secrets

from tumblrkit.core import record_event, confirm_membership

class LinkStore(Protocol):
    def create(self, campaign_id: str, source_post_id: str) -> str: ...
    def mark(self, event_id: str, event_type: str, link_id: str) -> bool: ...

class TelegramGate(Protocol):
    def verify_member(self, group_id: str, telegram_user_id: str) -> bool: ...

class TelegramPublisher(Protocol):
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

class CampaignHandoff:
    def __init__(self, links: LinkStore, gate: TelegramGate, publisher: TelegramPublisher,
                 *, campaign_group=None):
        self.links, self.gate, self.publisher = links, gate, publisher
        # Trusted owner-provided campaign->group binding, not an untrusted
        # group_id from a browser or a public referral URL.
        self.campaign_group = campaign_group

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
        if self.gate.verify_member(group_id, telegram_user_id) is True:
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
        if self.gate.verify_member(group_id,telegram_user_id) is not True:
            return MembershipAttribution("not_verified")
        grants=confirm_membership(db,campaign_id,telegram_user_id,
                                  gate_verified=True,referral_code=referral_code)
        return MembershipAttribution("member_verified",tuple(grants))

    def publish_announcement(self, destination: str, text: str, operation_key: str, *, confirmed: bool = False) -> HandoffResult:
        if not destination or not text or not operation_key:
            raise ValueError("destination/text/key required")
        if not confirmed:
            return HandoffResult("confirmation_required", ("confirm_publish", "edit_draft"))
        receipt = self.publisher.publish(destination, text, operation_key)
        # Unknown publication outcomes must be reconciled by the MPB owner
        # with the same operation key, never guessed into an identifier.
        message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
        if not isinstance(message_id, str) or not message_id:
            return HandoffResult("unverified", ("reconcile_delivery",))
        if not self.publisher.readback(destination, message_id):
            return HandoffResult("unverified", ("reconcile_delivery",), message_id)
        return HandoffResult("published", ("inspect_post", "measure_referrals"), message_id)
