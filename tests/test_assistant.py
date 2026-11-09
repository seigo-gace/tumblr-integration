import json
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant, ProviderError, ScriptedProvider, providers_from_env
from tumblrkit.bridge import Bridge, ActionRequest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA = "c" * 40
TOKEN = "test-access-token-1"


class RecordingAdapter:
    def __init__(self):
        self.calls = 0
    def perform(self, action, target, payload):
        self.calls += 1
        return {"verified": True, "provider_id": "fake"}


def post(url, token, payload):
    request = Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    })
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as error:
        return error.code, error.read()


class AssistantTests(unittest.TestCase):
    def test_chat_does_not_require_a_module_and_does_not_execute(self):
        result = Assistant(providers=[]).turn("募集の状況を分析して 対象 campaign-1")
        self.assertFalse(result["executed"])
        self.assertEqual("not_connected", result["state"])
        self.assertIn("実行していません", result["text"])

    def test_publish_needs_confirmation_and_confirm_stays_unconnected(self):
        assistant = Assistant(providers=[])
        preview = assistant.turn("下書きを公開して 対象 recruit-blog")
        self.assertEqual("confirmation_required", preview["state"])
        self.assertFalse(preview["executed"])
        self.assertIn("まだ実行していません", preview["text"])
        confirmed = assistant.turn(confirm=True, request_id=preview["request_id"])
        self.assertEqual("not_connected", confirmed["state"])
        self.assertFalse(confirmed["executed"])

    def test_unknown_tool_and_delete_are_not_called(self):
        adapter = RecordingAdapter()
        assistant = Assistant(bridge=Bridge({"tumblr": adapter}), providers=[
            ScriptedProvider("gemini", {"action": "tumblr.delete_blog", "target": "x"}),
        ])
        result = assistant.turn("消して")
        self.assertEqual("invalid_tool", result["state"])
        self.assertFalse(result["executed"])
        self.assertEqual(0, adapter.calls)
        refused = Assistant(providers=[]).turn("ブログを削除して")
        self.assertEqual("refused_unlisted", refused["state"])
        self.assertFalse(refused["executed"])

    def test_secret_text_is_removed_before_the_provider(self):
        provider = ScriptedProvider("gemini", {"action": "", "target": "", "question": ""})
        Assistant(providers=[provider]).turn("連絡は owner@example.com と bearer abcdefghijklmnop")
        self.assertNotIn("owner@example.com", provider.prompts[0])
        self.assertNotIn("abcdefghijklmnop", provider.prompts[0])

    def test_rate_limit_falls_back_once_to_free_provider_only(self):
        paid = ScriptedProvider("groq", error="rate_limited")
        other_paid = ScriptedProvider("gemini", {"action": "analytics.get_summary", "target": "week"})
        free = ScriptedProvider("openrouter", {"action": "analytics.get_summary", "target": "week"}, paid=False)
        result = Assistant(providers=[paid, other_paid, free]).turn("短文")
        self.assertEqual("not_connected", result["state"])
        self.assertFalse(result["executed"])
        self.assertEqual(1, len(paid.prompts))
        self.assertEqual([], other_paid.prompts)
        self.assertEqual(1, len(free.prompts))

    def test_paid_openrouter_model_is_rejected(self):
        with self.assertRaises(ProviderError):
            providers_from_env({
                "TUMBLR_OPENROUTER_API_KEY": "test-key-value-1234",
                "TUMBLR_OPENROUTER_MODEL": "openai/gpt-4o",
            })

    def test_confirmed_readback_completes_once(self):
        adapter = RecordingAdapter()
        assistant = Assistant(bridge=Bridge({"tumblr": adapter}), providers=[])
        preview = assistant.turn("下書きを公開して 対象 recruit-blog")
        done = assistant.turn(confirm=True, request_id=preview["request_id"])
        again = assistant.turn(confirm=True, request_id=preview["request_id"])
        self.assertTrue(done["executed"])
        self.assertEqual("completed", done["state"])
        self.assertFalse(again["executed"])
        self.assertEqual(1, adapter.calls)

    def test_http_chat_without_a_module_is_authorized_and_not_executed(self):
        dest = Path(self.id() + "-dist")
        release = build_release(ROOT / "app", dest, SHA)
        self.addCleanup(lambda: __import__("shutil").rmtree(dest, ignore_errors=True))
        with AppServer(release, TOKEN, assistant=Assistant(providers=[])) as server:
            status, body = post(f"http://127.0.0.1:{server.port}/assistant", TOKEN, {"message": "分析して 対象 week"})
        self.assertEqual(200, status)
        self.assertFalse(body["executed"])
        self.assertIn("実行していません", body["text"])

    def test_malformed_provider_payload_fails_closed(self):
        adapter = RecordingAdapter()
        provider = ScriptedProvider("gemini", {"action": ["tumblr.publish_draft"], "target": "demo"})
        result = Assistant(bridge=Bridge({"tumblr": adapter}), providers=[provider]).turn("投稿を希望")
        self.assertEqual("invalid_response", result["state"])
        self.assertFalse(result["executed"])
        self.assertTrue(result["recovery"])
        self.assertEqual(0, adapter.calls)

    def test_provider_returns_non_object_fails_closed(self):
        provider = ScriptedProvider("gemini", {"action": "tumblr.discover", "target": {"unsafe": True}})
        result = Assistant(providers=[provider]).turn("検索して")
        self.assertEqual("invalid_response", result["state"])
        self.assertFalse(result["executed"])

    def test_invalid_http_model_response_does_not_execute(self):
        from tumblrkit.assistant import HttpProvider
        class Transport:
            def post(self, url, headers, body):
                return 200, {"choices": [{"message": {"content": "[1,2,3]"}}]}
        provider = HttpProvider("groq", "https://example.invalid", {}, False, Transport())
        result = Assistant(providers=[provider]).turn("検索して")
        self.assertEqual("invalid_response", result["state"])
        self.assertFalse(result["executed"])

    def test_transport_unavailable_replies_without_execution(self):
        from tumblrkit.assistant import UrllibTransport
        from unittest.mock import patch
        from urllib.error import URLError
        with patch("tumblrkit.assistant.urlopen", side_effect=URLError("offline")):
            provider = providers_from_env({"TUMBLR_GROQ_API_KEY": "test-key"}, transport=UrllibTransport())[0]
            result = Assistant(providers=[provider]).turn("検索して")
        self.assertEqual("network_unavailable", result["state"])
        self.assertFalse(result["executed"])
        self.assertTrue(result["recovery"])


if __name__ == "__main__":
    unittest.main()
