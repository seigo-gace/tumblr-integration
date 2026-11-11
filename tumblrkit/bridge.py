"""Contract-level bridge from approved AI actions to external adapters.

External APIs are deliberately not implemented here. Production adapters must
verify caller identity, OAuth scope, provider permissions and actual readbacks.
"""
from dataclasses import dataclass, field
from typing import Protocol, Any

ALLOWED = frozenset({
    "tumblr.discover", "tumblr.list_posts", "tumblr.publish_draft",
    "tumblr.edit_post", "tumblr.delete_post",
    "telegram.check_membership", "telegram.publish_announcement",
    "analytics.get_summary",
    "engagement.like", "engagement.reblog", "engagement.follow",
})
CONFIRM = frozenset({"tumblr.publish_draft", "tumblr.edit_post",
                     "tumblr.delete_post", "telegram.publish_announcement",
                     "engagement.like", "engagement.reblog", "engagement.follow"})

class Adapter(Protocol):
    def perform(self, action: str, target: str, payload: dict[str, Any]) -> dict[str, Any]: ...

@dataclass(frozen=True)
class ActionRequest:
    request_id: str
    action: str
    target: str
    payload: dict[str, Any] = field(default_factory=dict)
    confirmed: bool = False

@dataclass(frozen=True)
class ActionResult:
    request_id: str
    action: str
    state: str
    detail: dict[str, Any] = field(default_factory=dict)

class Bridge:
    def __init__(self, adapters: dict[str, Adapter]):
        self.adapters = adapters
        self.results: dict[str, ActionResult] = {}

    def dispatch(self, request: ActionRequest) -> ActionResult:
        if not request.request_id or not request.target:
            raise ValueError("request_id and target required")
        if request.action not in ALLOWED:
            raise ValueError("action is not authorized")
        previous = self.results.get(request.request_id)
        if previous:
            if previous.action != request.action:
                raise ValueError("request_id reused for another action")
            return previous
        if request.action in CONFIRM and not request.confirmed:
            return ActionResult(request.request_id, request.action, "confirmation_required")
        provider = request.action.split(".", 1)[0]
        adapter = self.adapters.get(provider)
        if not adapter:
            return ActionResult(request.request_id, request.action, "not_connected")
        try:
            data = adapter.perform(request.action, request.target, request.payload)
        except Exception as exc:
            # Only fixed, known provider codes cross the trust boundary.
            # Never surface arbitrary exception messages or credentials.
            codes=frozenset({
                "tumblr_auth_expired","tumblr_permission_denied",
                "tumblr_rate_limited","oauth_not_connected",
                "oauth_refresh_unavailable","oauth_token_permissions",
                "tumblr_unavailable",
                "tumblr_publish_reconcile_required",
                "tumblr_publish_draft_changed",
                "tumblr_publish_uncertain",
                "tumblr_publish_readback_unverified",
                "tumblr_publish_rejected",
                "tumblr_write_consent_required",
                "tumblr_post_target_invalid",
                "tumblr_post_not_verified",
                "tumblr_post_already_deleted",
                "tumblr_post_reconcile_required",
                "tumblr_post_revision_missing",
                "tumblr_post_revision_invalid",
                "tumblr_post_approval_stale",
                "tumblr_post_readback_unverified",
                "tumblr_post_external_changed",
                "tumblr_mutation_uncertain",
                "tumblr_mutation_rejected",
                "engagement_target_missing",
                "engagement_target_invalid",
                "engagement_provider_refused",
                "engagement_post_uncertain",
                "engagement_readback_unverified",
                "engagement_reconcile_required",
            })
            code=str(exc) if isinstance(exc,ValueError) else ""
            detail={"error_type":type(exc).__name__}
            if code in codes:
                detail["error_code"]=code
            return ActionResult(request.request_id, request.action, "failed", detail)
        # An adapter must return an explicit, provider-read-back verification.
        if not isinstance(data, dict) or data.get("verified") is not True:
            return ActionResult(request.request_id, request.action, "unverified")
        result = ActionResult(request.request_id, request.action, "completed", data)
        self.results[request.request_id] = result
        return result
