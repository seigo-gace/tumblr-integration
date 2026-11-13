import tempfile
import unittest
from pathlib import Path

from tumblrkit.bridge import ActionRequest, Bridge
from tumblrkit.telegram_bridge_client import LocalTelegramOwner, telegram_owner_from_env


class TelegramOwnerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = Path(self.temp.name) / "token"
        self.key.write_text("x" * 40, encoding="utf-8")
        self.calls = []

    def _transport(self, payload):
        self.calls.append(payload)
        if payload["action"] == "member":
            return {"ok": True, "verified": True}
        return {"ok": True, "verified": True, "message_id": "987"}

    def test_existing_member_and_post_use_one_owner_connection(self):
        owner = LocalTelegramOwner(self.key, member_id="123", transport=self._transport)
        bridge = Bridge({"telegram": owner})
        member = bridge.dispatch(ActionRequest("m", "telegram.check_membership", "4"))
        self.assertEqual(member.state, "completed")
        self.assertEqual(self.calls[0],
                         {"action": "member", "destination": "4", "user_id": "123"})
        draft = ActionRequest("one-id", "telegram.publish_announcement", "4",
                              payload={"text": "告知本文"}, confirmed=True)
        self.assertEqual(bridge.dispatch(draft).state, "completed")
        self.assertEqual(bridge.dispatch(draft).state, "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["operation_key"], "one-id")
        self.assertEqual(self.calls[1]["text"], "告知本文")

    def test_no_settings_no_external_call_and_incorrect_result_not_completed(self):
        self.assertIsNone(telegram_owner_from_env({}))
        owner = LocalTelegramOwner(self.key,
              transport=lambda payload: {"ok": True, "verified": False})
        result = Bridge({"telegram": owner}).dispatch(
            ActionRequest("post-1", "telegram.publish_announcement", "4",
                          payload={"text": "x"}, confirmed=True))
        self.assertEqual(result.state, "unverified")
        self.assertEqual(LocalTelegramOwner(self.key).member_id, None)


if __name__ == "__main__":
    unittest.main()
