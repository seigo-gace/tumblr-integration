"""Purpose-specific Tumblr↔Telegram integration; external adapters remain owned elsewhere."""
from dataclasses import dataclass
from typing import Protocol

class LinkStore(Protocol):
    def create(self, campaign_id: str, source_post_id: str) -> str: ...
    def mark(self, event_id: str, event_type: str, link_id: str) -> bool: ...

class TelegramGate(Protocol):
    def verify_member(self, group_id: str, telegram_user_id: str) -> bool: ...

class TelegramPublisher(Protocol):
    def publish(self, destination: str, text: str, operation_key: str) -> dict: ...
    def readback(self, destination: str, message_id: str) -> bool: ...

@dataclass(frozen=True)
class HandoffResult:
    status: str
    next_actions: tuple[str, ...]
    reference: str | None = None

class CampaignHandoff:
    def __init__(self, links: LinkStore, gate: TelegramGate, publisher: TelegramPublisher):
        self.links, self.gate, self.publisher = links, gate, publisher

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
        if self.gate.verify_member(group_id, telegram_user_id):
            return HandoffResult("member_verified", ("evaluate_campaign_eligibility",))
        return HandoffResult("not_verified", ("show_join_instructions", "retry_verification"))

    def publish_announcement(self, destination: str, text: str, operation_key: str, *, confirmed: bool = False) -> HandoffResult:
        if not destination or not text or not operation_key:
            raise ValueError("destination/text/key required")
        if not confirmed:
            return HandoffResult("confirmation_required", ("confirm_publish", "edit_draft"))
        receipt = self.publisher.publish(destination, text, operation_key)
        message_id = receipt.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            return HandoffResult("unverified", ("reconcile_delivery",))
        if not self.publisher.readback(destination, message_id):
            return HandoffResult("unverified", ("reconcile_delivery",), message_id)
        return HandoffResult("published", ("inspect_post", "measure_referrals"), message_id)
