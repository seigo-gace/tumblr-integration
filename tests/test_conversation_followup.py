"""Regression contract for independent authenticated conversation follow-ups.

Only loopback HTTP and a recording local operation adapter are used.
The tests deliberately exercise the existing /assistant request_id contract.
"""
import json
import tempfile
import unittest
from pathlib import Path

from test_app_serve import APP, SHA_A, TOKEN, fetch
from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant


class ConversationFollowupTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        release = build_release(APP, Path(work.name), SHA_A)
        self.calls = []
        assistant = Assistant(providers=[])
        self.server = AppServer(release, TOKEN, assistant=assistant)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        assistant.catalog_perform = self.record
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.a = self.login()
        self.b = self.login()
        self.assertNotEqual(self.a, self.b)

    def record(self, action, payload):
        self.calls.append((action, dict(payload)))
        return {"verified": True, "summary": "mock result", "published": False}

    def login(self):
        status, _, headers = fetch(
            self.base + "/session", method="POST",
            data=b"token=" + TOKEN.encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 204)
        return headers["Set-Cookie"].split(";", 1)[0]

    def turn(self, cookie, **payload):
        status, body, _ = fetch(
            self.base + "/assistant", method="POST",
            data=json.dumps(payload).encode(),
            headers={"Cookie": cookie, "Content-Type": "application/json",
                     "Origin": self.base},
        )
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def ask(self, cookie, message):
        result = self.turn(cookie, message=message)
        self.assertIn(result["state"], ("guidance", "input_required"))
        self.assertFalse(result["executed"])
        self.assertTrue(result.get("request_id"), result)
        return result["request_id"]

    def confirmation(self, cookie, campaign):
        result = self.turn(cookie, message="当選者へ通知\n企画ID: " + campaign
                           + "\n通知ID: event-1")
        self.assertEqual(result["state"], "confirmation_required", result)
        return result["request_id"]

    def test_interleaved_inputs_remain_in_their_own_sessions(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        b = self.ask(self.b, "投稿文をゼロから作る")
        self.assertNotEqual(a, b)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.b, message="Bの本文", request_id=b)["state"],
                         "completed")
        self.assertEqual(self.turn(self.a, message="Aの本文", request_id=a)["state"],
                         "completed")
        self.assertEqual(self.calls, [
            ("publisher.01", {"text": "Bの本文"}),
            ("publisher.01", {"text": "Aの本文"}),
        ])

    def test_foreign_input_id_cannot_complete_or_consume_owner_input(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        result = self.turn(self.b, message="別セッションの本文", request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, message="本人の本文", request_id=a)["state"],
                         "completed")
        self.assertEqual(self.calls, [("publisher.01", {"text": "本人の本文"})])

    def test_foreign_confirmation_cannot_execute_or_consume_owner_request(self):
        a = self.confirmation(self.a, "campaign-a")
        result = self.turn(self.b, confirm=True, request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, confirm=True, request_id=a)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], "campaign.08")
        self.assertEqual(self.calls[0][1]["campaign_id"], "campaign-a")
        self.turn(self.a, confirm=True, request_id=a)
        self.assertEqual(len(self.calls), 1)

    def test_abort_is_scoped_to_own_conversation(self):
        a = self.ask(self.a, "投稿文をゼロから作る")
        b = self.confirmation(self.b, "campaign-b")
        self.turn(self.a, abort=True, request_id=a)
        result = self.turn(self.a, message="中断済み本文", request_id=a)
        self.assertNotEqual(result["state"], "completed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.b, confirm=True, request_id=b)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["campaign_id"], "campaign-b")

    def test_foreign_abort_does_not_clear_owner_confirmation(self):
        a = self.confirmation(self.a, "campaign-a")
        self.turn(self.b, abort=True, request_id=a)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.turn(self.a, confirm=True, request_id=a)["state"],
                         "completed")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
