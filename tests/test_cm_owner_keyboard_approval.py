"""Existing URL buttons survive the owner Assistant preview and MPB send."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.cm_links import CMRegistry
from tumblrkit.telegram_bridge_client import LocalTelegramOwner


class CMOwnerKeyboardApprovalTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        secret=Path(temporary.name)/"key"
        secret.write_text("x"*40)
        db=sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        registry=CMRegistry(db)
        registry.add("ad1","スポンサー","https://sponsor.example.org")
        self.requests=[]
        def transport(data):
            self.requests.append(data)
            if data["action"]=="lookup":
                return {"verified":False}
            if data["action"]=="publish":
                return {"verified":True,"message_id":"101"}
            if data["action"]=="readback":
                published=next(x for x in self.requests if x["action"]=="publish")
                return {"verified":True,"inline_keyboard":
                        published.get("buttons",[])+published.get("cm_buttons",[])}
            raise AssertionError(data)
        telegram=LocalTelegramOwner(secret,transport=transport,cm_registry=registry)
        self.assistant=Assistant(bridge=Bridge({"telegram":telegram}),providers=[])
        self.original=[[{"text":"応募","url":"https://example.org/apply"}],
                       [{"text":"詳細","url":"https://example.org/guide"}]]

    def test_preview_includes_original_and_pr_and_confirm_sends_exact_rows(self):
        proposed=self.assistant.turn(
            selected_action="telegram.publish_announcement",
            selected_target="100",
            message="告知 対象: 100\n本文: 新しいお知らせ",
            existing_buttons=self.original)
        self.assertEqual(proposed["state"],"confirmation_required")
        self.assertIn("応募",proposed["text"])
        self.assertIn("詳細",proposed["text"])
        self.assertIn("PR / スポンサー",proposed["text"])
        self.assertEqual(self.requests,[])
        published=self.assistant.turn(confirm=True,request_id=proposed["request_id"])
        self.assertEqual(published["state"],"completed")
        request=next(x for x in self.requests if x["action"]=="publish")
        self.assertEqual(request["buttons"],self.original)
        self.assertEqual(request["cm_buttons"],
                         [[{"text":"PR / スポンサー",
                            "url":"https://sponsor.example.org"}]])

    def test_invalid_original_rows_do_not_publish(self):
        for rows in (
            [[{"text":"秘密","callback_data":"bypass"}]],
            [[{"text":"ローカル","url":"https://127.0.0.1/private"}]],
            [[{"text":"HTTP","url":"http://example.org"}]],
            [[{"text":"複数","url":"https://example.org","extra":"unapproved"}]],
        ):
            with self.subTest(rows=rows):
                outcome=self.assistant.turn(
                    selected_action="telegram.publish_announcement",
                    selected_target="100",message="告知 対象: 100\n本文: お知らせ",
                    existing_buttons=rows)
                self.assertEqual(outcome["state"],"invalid_tool")
        self.assertEqual(self.requests,[])


if __name__=="__main__":
    unittest.main()
