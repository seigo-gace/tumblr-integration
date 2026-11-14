import sqlite3
import tempfile
import unittest
from pathlib import Path

from tumblrkit.bridge import ActionRequest, Bridge
from tumblrkit.cm_links import CMRegistry
from tumblrkit.telegram_bridge_client import LocalTelegramOwner


class CMOwnerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "key"
        self.token.write_text("x" * 40)
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.cm = CMRegistry(self.db)
        for i in range(4):
            self.cm.add(f"c{i}", f"案内{i}", f"https://example.org/{i}")
        self.requests = []

    def deliver(self, *, keyboard=True, receipt_verified=True):
        def transport(payload):
            self.requests.append(payload)
            if payload["action"] == "lookup":
                return {"verified": False}
            if payload["action"] == "publish":
                return {"verified": True, "message_id": "77"}
            if payload["action"] == "readback":
                rows = next(v["cm_buttons"] for v in self.requests if v["action"] == "publish")
                return {"verified": receipt_verified,
                        "inline_keyboard": [[{"text": "既存", "callback_data": "go"}]] + rows if keyboard else []}
            raise AssertionError(payload)
        owner = LocalTelegramOwner(self.token, transport=transport, cm_registry=self.cm)
        return Bridge({"telegram": owner}).dispatch(
            ActionRequest("cm-op-1", "telegram.publish_announcement", "100",
                          {"text": "告知"}, confirmed=True))

    def test_verified_keyboard_advances_once(self):
        result = self.deliver()
        self.assertEqual(result.state, "completed")
        self.assertEqual([x["id"] for x in self.cm.select("100")], ["c1","c2","c3"])
        sent = next(x for x in self.requests if x["action"] == "publish")
        self.assertEqual(len(sent["cm_buttons"]), 3)
        self.assertEqual(sent["cm_buttons"][0][0]["text"], "PR / 案内0")

    def test_recovery_reuses_pinned_selection_after_cursor_moves(self):
        self.assertEqual(self.deliver().state, "completed")
        chosen = self.cm.select_for_operation("100", "cm-op-1")
        self.assertEqual([x["id"] for x in chosen], ["c0", "c1", "c2"])
        self.assertTrue(self.cm.is_advanced("100", "cm-op-1",
                                           ["c0", "c1", "c2"]))
        self.assertEqual([x["id"] for x in self.cm.select("100")],
                         ["c1", "c2", "c3"])

    def test_persistent_store_reopens_per_operation(self):
        path = Path(self.temp.name) / "drafts.sqlite3"
        import sqlite3
        with sqlite3.connect(path) as db:
            stored = CMRegistry(db)
            stored.add("persisted", "永続CM", "https://example.org/persisted")
        calls = []
        def transport(payload):
            calls.append(payload)
            if payload["action"] == "lookup":
                return {"verified": False}
            if payload["action"] == "publish":
                return {"verified": True, "message_id": "91"}
            if payload["action"] == "readback":
                rows = next(x["cm_buttons"] for x in calls if x["action"] == "publish")
                return {"verified": True, "inline_keyboard": rows}
        owner = LocalTelegramOwner(self.token, transport=transport,
                                   cm_store_path=path)
        result = owner.perform("telegram.publish_announcement", "100",
                               {"text": "本文", "operation_key": "persistent-op"})
        self.assertTrue(result["verified"])
        with sqlite3.connect(path) as db:
            self.assertEqual(CMRegistry(db).select("100")[0]["id"], "persisted")
            self.assertTrue(CMRegistry(db).is_advanced(
                "100", "persistent-op", ["persisted"]))
        self.assertEqual(calls[1]["cm_buttons"][0][0]["text"], "PR / 永続CM")

    def test_missing_keyboard_does_not_claim_success_or_advance(self):
        result = self.deliver(keyboard=False)
        self.assertEqual(result.state, "unverified")
        self.assertEqual([x["id"] for x in self.cm.select("100")], ["c0","c1","c2"])

    def test_unverified_receipt_does_not_advance(self):
        self.assertEqual(self.deliver(receipt_verified=False).state, "unverified")
        self.assertEqual([x["id"] for x in self.cm.select("100")], ["c0","c1","c2"])


if __name__ == "__main__":
    unittest.main()
