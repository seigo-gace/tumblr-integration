"""Contract-level bridge from approved AI actions to external adapters.

External APIs are deliberately not implemented here. Production adapters must
verify caller identity, OAuth scope, provider permissions and actual readbacks.
"""
from dataclasses import dataclass, field
from typing import Protocol, Any

ALLOWED = frozenset({
    "tumblr.discover", "tumblr.list_posts", "tumblr.publish_draft",
    "telegram.check_membership", "telegram.publish_announcement",
    "analytics.get_summary",
})
CONFIRM = frozenset({"tumblr.publish_draft", "telegram.publish_announcement"})

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
            # Avoid logging exception bodies or secrets in user-facing replies.
            return ActionResult(request.request_id, request.action, "failed", {"error_type": type(exc).__name__})
        # An adapter must return an explicit, provider-read-back verification.
        if not isinstance(data, dict) or data.get("verified") is not True:
            return ActionResult(request.request_id, request.action, "unverified")
        result = ActionResult(request.request_id, request.action, "completed", data)
        self.results[request.request_id] = result
        return result
