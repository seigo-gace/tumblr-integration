"""One local call into the existing GayBBS GATE/MPB owner.

The running Bot owns Telegram credentials, approvals, verified topics and
durable delivery receipts. Tumblr does not duplicate these databases.
"""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from tumblrkit.cm_links import append_cm_buttons


class LocalTelegramOwner:
    def __init__(self, token_file, *, endpoint="http://127.0.0.1:8876/internal/tumblr",
                 member_id=None, transport=None, cm_registry=None):
        if endpoint != "http://127.0.0.1:8876/internal/tumblr":
            raise ValueError("telegram_owner_endpoint_invalid")
        self.token = Path(token_file).read_text(encoding="utf-8").strip()
        if len(self.token) < 32:
            raise ValueError("telegram_owner_token_invalid")
        self.endpoint = endpoint
        self.member_id = member_id
        self.transport = transport or self._post
        self.cm_registry = cm_registry

    def _post(self, value):
        request = Request(self.endpoint,
                          json.dumps(value, ensure_ascii=False).encode("utf-8"),
                          {"Content-Type": "application/json",
                           "Authorization": "Bearer " + self.token},
                          method="POST")
        with urlopen(request, timeout=8) as response:
            if response.status != 200:
                raise ValueError("telegram_owner_unavailable")
            return json.load(response)

    def perform(self, action, target, payload):
        if action == "telegram.check_membership":
            # The owner identity must come from the private app configuration.
            # In a future user-bound flow, the trusted server may inject it.
            if ":" in target:
                destination, user_id = target.split(":", 1)
            else:
                destination, user_id = target, self.member_id
            if not user_id:
                return {"verified": False, "status": "not_connected"}
            result = self.transport({"action": "member",
                                     "destination": destination,
                                     "user_id": str(user_id)})
            return {"verified": result.get("verified") is True,
                    "status": "member_verified" if result.get("verified") is True
                              else "not_verified"}
        if action == "telegram.publish_announcement":
            text, operation = payload.get("text"), payload.get("operation_key")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("announcement_text_required")
            if not isinstance(operation, str) or not operation:
                raise ValueError("announcement_operation_key_required")
            cm_selection = self.cm_registry.select(target) if self.cm_registry else []
            cm_rows = append_cm_buttons([], cm_selection) if cm_selection else []
            # Existing MPB operation ledger is the recovery source. Check it
            # first, so a retry after a lost response only reads the post.
            try:
                previous = self.transport({"action": "lookup",
                                           "destination": target,
                                           "operation_key": operation})
            except (OSError, ValueError, TypeError, KeyError):
                return {"verified": False, "status": "unverified", "reference": None}
            message = previous.get("message_id") if isinstance(previous, dict) and previous.get("verified") is True else None
            if not isinstance(message, str) or not message:
                # No existing sent receipt: ask the same MPB owner to publish,
                # always reusing the Bridge's stable operation key.
                try:
                    result = self.transport({"action": "publish",
                                             "destination": target,
                                             "text": text,
                                             "operation_key": operation,
                                             **({"cm_buttons": cm_rows} if cm_rows else {})})
                except (OSError, ValueError, TypeError, KeyError):
                    return {"verified": False, "status": "unverified", "reference": None}
                message = result.get("message_id") if isinstance(result, dict) else None
                if not isinstance(result, dict) or result.get("verified") is not True or not isinstance(message, str) or not message:
                    return {"verified": False, "status": "unverified", "reference": None}
            # A response from publish/lookup alone cannot prove delivery.
            # The existing Bot's owner-bound readback is authoritative.
            try:
                receipt = self.transport({"action": "readback",
                                          "destination": target,
                                          "message_id": message})
            except (OSError, ValueError, TypeError, KeyError):
                return {"verified": False, "status": "unverified", "reference": message}
            verified = isinstance(receipt, dict) and receipt.get("verified") is True
            if verified and cm_rows:
                keyboard = receipt.get("inline_keyboard")
                verified = (isinstance(keyboard, list) and
                            len(keyboard) >= len(cm_rows) and
                            keyboard[-len(cm_rows):] == cm_rows)
                if verified:
                    try:
                        self.cm_registry.advance_after_delivery(
                            target, operation, [item["id"] for item in cm_selection],
                            verified=True)
                    except ValueError:
                        verified = False
            return {"verified": verified,
                    "status": "published" if verified else "unverified",
                    "reference": message}
        raise ValueError("telegram_owner_action_invalid")


def telegram_owner_from_env(env=None):
    env = os.environ if env is None else env
    key_file = env.get("TUMBLR_BRIDGE_KEY_FILE", "")
    if not key_file:
        existing = Path.home() / ".config" / "tgtumbler" / "bridge" / "owner-token"
        if not existing.is_file():
            return None
        key_file = str(existing)
    return LocalTelegramOwner(key_file,
                              member_id=env.get("TUMBLR_TELEGRAM_MEMBER_ID"))
