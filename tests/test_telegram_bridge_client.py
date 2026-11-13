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
        if payload["action"] == "lookup":
            return {"ok": True, "verified": False}
        if payload["action"] == "readback":
            return {"ok": True, "verified": True}
        return {"ok": True, "verified": True, "message_id": "987"}

    def test_existing_member_and_post_use_one_owner_connection(self):
        owner = LocalTelegramOwner(self.key, member_id="123", transport=self._transport)
        bridge = Bridge({"telegram": owner})
        member = bridge.dispatch(ActionRequest("m", "telegram.check_membership", "4"))
        self.assertEqual(member.state, "completed")
        self.assertEqual(self.calls[0],
                         {"action": "member", "destination": "4", "user_id": "123"})
        second = Bridge({"telegram":LocalTelegramOwner(
            self.key, transport=self._transport)}).dispatch(
                ActionRequest("m2", "telegram.check_membership", "4:456"))
        self.assertEqual(second.state, "completed")
        self.assertEqual(self.calls[-1],
                         {"action": "member", "destination": "4", "user_id": "456"})

        draft = ActionRequest("one-id", "telegram.publish_announcement", "4",
                              payload={"text": "告知本文"}, confirmed=True)
        self.assertEqual(bridge.dispatch(draft).state, "completed")
        self.assertEqual(bridge.dispatch(draft).state, "completed")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.calls[2],
                         {"action": "lookup", "destination": "4", "operation_key": "one-id"})
        self.assertEqual(self.calls[3]["operation_key"], "one-id")
        self.assertEqual(self.calls[3]["text"], "告知本文")
        self.assertEqual(self.calls[4],
                         {"action": "readback", "destination": "4", "message_id": "987"})

    def test_publish_requires_owner_readback_and_retains_reference(self):
        for readback in ({"ok": True, "verified": False},
                         {"ok": True, "verified": 1}, None):
            with self.subTest(readback=readback):
                calls = []
                def transport(payload):
                    calls.append(payload["action"])
                    if payload["action"] == "lookup":
                        return {"ok": True, "verified": False}
                    if payload["action"] == "publish":
                        return {"ok": True, "verified": True, "message_id": "987"}
                    return readback
                owner = LocalTelegramOwner(self.key, transport=transport)
                result = Bridge({"telegram": owner}).dispatch(
                    ActionRequest("post-readback", "telegram.publish_announcement", "4",
                                  payload={"text": "確認"}, confirmed=True))
                self.assertEqual(result.state, "unverified")
                self.assertEqual(calls, ["lookup", "publish", "readback"])

    def test_publish_readback_transport_failure_is_not_success(self):
        def transport(payload):
            if payload["action"] == "lookup":
                return {"ok": True, "verified": False}
            if payload["action"] == "publish":
                return {"ok": True, "verified": True, "message_id": "987"}
            raise OSError("owner unavailable")
        owner = LocalTelegramOwner(self.key, transport=transport)
        self.assertEqual(Bridge({"telegram": owner}).dispatch(
            ActionRequest("post-owner-down", "telegram.publish_announcement", "4",
                          payload={"text": "確認"}, confirmed=True)).state, "unverified")

    def test_recovery_uses_existing_owner_ledger_without_reposting(self):
        calls = []
        def transport(payload):
            calls.append(payload)
            if payload["action"] == "lookup":
                return {"ok": True, "verified": True, "message_id": "321"}
            if payload["action"] == "readback":
                return {"ok": True, "verified": True}
            self.fail("publish called for an already-delivered operation")
        owner = LocalTelegramOwner(self.key, transport=transport)
        result = Bridge({"telegram": owner}).dispatch(
            ActionRequest("existing-id", "telegram.publish_announcement", "4",
                          payload={"text": "既存の告知"}, confirmed=True))
        self.assertEqual(result.state, "completed")
        self.assertEqual(result.detail["reference"], "321")
        self.assertEqual([call["action"] for call in calls], ["lookup", "readback"])
        self.assertEqual(calls[0]["operation_key"], "existing-id")

    def test_lookup_outage_does_not_issue_new_post(self):
        calls = []
        def transport(payload):
            calls.append(payload["action"])
            raise OSError("owner unavailable")
        owner = LocalTelegramOwner(self.key, transport=transport)
        result = Bridge({"telegram": owner}).dispatch(
            ActionRequest("uncertain-id", "telegram.publish_announcement", "4",
                          payload={"text": "同一操作"}, confirmed=True))
        self.assertEqual(result.state, "unverified")
        self.assertEqual(calls, ["lookup"])

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
