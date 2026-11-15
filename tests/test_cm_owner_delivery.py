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
                        "inline_keyboard": rows if keyboard else []}
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

    def test_preview_and_publish_keep_original_selection_after_other_post(self):
        original = self.cm.select_for_operation("100", "later")
        first = self.cm.select_for_operation("100", "earlier")
        self.cm.advance_after_delivery(
            "100", "earlier", [x["id"] for x in first], verified=True)
        self.assertEqual(self.cm.select_for_operation("100", "later"), original)
        self.assertTrue(self.cm.advance_after_delivery(
            "100", "later", [x["id"] for x in original], verified=True))
        self.assertEqual([x["id"] for x in self.cm.select("100")],
                         ["c2", "c3", "c0"])

    def test_stopped_cm_cannot_be_published_from_old_preview(self):
        self.cm.select_for_operation("100", "stale")
        self.cm.set_enabled("c0", False)
        with self.assertRaisesRegex(ValueError, "cm_selection_stale"):
            self.cm.select_for_operation("100", "stale")

    def test_explicit_cm_disabled_keeps_existing_text_only_delivery(self):
        calls = []
        def transport(payload):
            calls.append(payload)
            if payload["action"] == "lookup":
                return {"verified": False}
            if payload["action"] == "publish":
                return {"verified": True, "message_id": "90"}
            if payload["action"] == "readback":
                return {"verified": True}
            raise AssertionError(payload)
        owner = LocalTelegramOwner(self.token, transport=transport,
                                   cm_registry=self.cm)
        result = owner.perform("telegram.publish_announcement", "100",
                               {"text": "CM非掲載", "operation_key": "no-cm",
                                "cm_disabled": True})
        self.assertTrue(result["verified"])
        publish = next(x for x in calls if x["action"] == "publish")
        self.assertNotIn("cm_buttons", publish)
        self.assertEqual([x["id"] for x in self.cm.select("100")],
                         ["c0", "c1", "c2"])

    def test_existing_rows_come_first_and_full_keyboard_is_verified(self):
        original = [[{"text": "応募はこちら", "url": "https://example.org/apply"}],
                    [{"text": "案内", "url": "https://example.org/info"}]]
        calls = []
        def transport(request):
            calls.append(request)
            if request["action"] == "lookup":
                return {"verified": False}
            if request["action"] == "publish":
                return {"verified": True, "message_id": "88"}
            if request["action"] == "readback":
                published = next(x for x in calls if x["action"] == "publish")
                return {"verified": True,
                        "inline_keyboard": published["buttons"] + published["cm_buttons"]}
            raise AssertionError(request)
        owner = LocalTelegramOwner(self.token, transport=transport, cm_registry=self.cm)
        receipt = owner.perform("telegram.publish_announcement", "100",
                                {"text": "本文", "operation_key": "combined",
                                 "buttons": original})
        self.assertTrue(receipt["verified"])
        posted = next(x for x in calls if x["action"] == "publish")
        self.assertEqual(posted["buttons"], original)
        self.assertEqual(len(posted["cm_buttons"]), 3)
        self.assertEqual(self.cm.select("100")[0]["id"], "c1")

    def test_missing_original_button_is_unverified_without_rotation(self):
        original = [[{"text": "応募", "url": "https://example.org/apply"}]]
        def transport(request):
            if request["action"] == "lookup":
                return {"verified": False}
            if request["action"] == "publish":
                return {"verified": True, "message_id": "99"}
            if request["action"] == "readback":
                return {"verified": True, "inline_keyboard": [
                    [{"text": f"PR / 案内{i}", "url": f"https://example.org/{i}"}]
                    for i in range(3)]}
            raise AssertionError(request)
        owner = LocalTelegramOwner(self.token, transport=transport, cm_registry=self.cm)
        receipt = owner.perform("telegram.publish_announcement", "100",
                                {"text": "本文", "operation_key": "lost-original",
                                 "buttons": original})
        self.assertFalse(receipt["verified"])
        self.assertEqual(self.cm.select("100")[0]["id"], "c0")

    def test_local_and_literal_ip_cm_links_never_enter_registry(self):
        for url in ("https://localhost/admin", "https://sub.localhost/data",
                    "https://10.0.0.1/private", "https://127.0.0.1/",
                    "https://[::1]/", "https://printer.local/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "cm_url_invalid"):
                    self.cm.add("unsafe", "禁止", url)
        self.assertEqual(len(self.cm.list_links()), 4)

    def test_missing_keyboard_does_not_claim_success_or_advance(self):
        result = self.deliver(keyboard=False)
        self.assertEqual(result.state, "unverified")
        self.assertEqual([x["id"] for x in self.cm.select("100")], ["c0","c1","c2"])

    def test_unverified_receipt_does_not_advance(self):
        self.assertEqual(self.deliver(receipt_verified=False).state, "unverified")
        self.assertEqual([x["id"] for x in self.cm.select("100")], ["c0","c1","c2"])


if __name__ == "__main__":
    unittest.main()
