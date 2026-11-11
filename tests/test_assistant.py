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
        from types import SimpleNamespace
        self.calls = 0
        self.publisher=SimpleNamespace(drafts=SimpleNamespace(
            get=lambda target: {"text":"公開前に確認する固定下書き文"}
        ))
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
    def test_verified_results_have_stable_columns_and_bounded_rows(self):
        class ListAdapter:
            def perform(self, action, target, payload):
                return {"verified": True, "rows": [
                    {"title": "投稿A", "type": "video", "date": "2026-11-11",
                     "url": "https://example.org/post", "unexpected": "not exposed"}
                ]}
        assistant = Assistant(bridge=Bridge({"tumblr": ListAdapter()}), providers=[])
        result = assistant.turn("投稿一覧を見せて 対象 myblog")
        self.assertTrue(result["executed"])
        view = result["view"]
        self.assertEqual(["title", "type", "date", "thumbnail_url", "url"], view["columns"])
        self.assertEqual("投稿A", view["rows"][0]["title"])
        self.assertNotIn("unexpected", view["rows"][0])

    def test_unconnected_action_does_not_return_fabricated_view(self):
        result = Assistant(providers=[]).turn("投稿一覧を見せて 対象 myblog")
        self.assertEqual("not_connected", result["state"])
        self.assertNotIn("view", result)

    def test_rendered_rows_are_bounded_and_only_expected_columns(self):
        view = Assistant._result_view("analytics.get_summary", {
            "rows": [{"label": "閲覧", "value": i, "unexpected": "x"} for i in range(150)]
        })
        self.assertEqual(100, len(view["rows"]))
        self.assertEqual(150, view["total"])
        self.assertTrue(view["truncated"])
        self.assertEqual(["label", "value", "period", "url"], view["columns"])

    def test_assistant_draft_requires_persistent_store_not_ephemeral_success(self):
        standalone=Assistant(providers=[])
        response=standalone.turn("保存されていない本文",draft=True)
        self.assertEqual(response["state"],"not_connected")
        self.assertFalse(response["executed"])
        self.assertFalse(hasattr(standalone,"drafts"))

    def test_assistant_draft_is_same_persistent_owner_draft_as_ui(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            release=build_release(ROOT/"app",Path(folder),SHA)
            owner_assistant=Assistant(providers=[])
            with AppServer(release,TOKEN,assistant=owner_assistant) as server:
                status,result=post(f"http://127.0.0.1:{server.port}/assistant",
                                   TOKEN,{"message":"共通の永続下書き","draft":True})
                self.assertEqual(status,200)
                self.assertEqual(result["state"],"draft_saved")
                self.assertFalse(result["executed"])
                draft_id=result["draft_id"]
                self.assertRegex(draft_id,r"^[0-9a-f]{32}$")
                self.assertEqual(server.httpd.draft_store.get(draft_id)["text"],
                                 "共通の永続下書き")
                status,body=post(f"http://127.0.0.1:{server.port}/assistant",
                                 TOKEN,{"message":"","draft":True})
                self.assertEqual(status,200)
                self.assertEqual(body["state"],"failed")
            from tumblrkit.drafts import DraftStore
            reopened=DraftStore(Path(folder)/"data"/"drafts.sqlite3")
            self.assertEqual(reopened.get(draft_id)["text"],"共通の永続下書き")

    def test_six_goals_not_flattened_into_single_action_or_omitted(self):
        adapter=RecordingAdapter()
        assistant=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[])
        result=assistant.turn(goals=[
            "巡回","投稿","キャンペーン","紹介","集計","メディア"])
        self.assertEqual(result["state"],"guidance")
        self.assertFalse(result["executed"])
        self.assertEqual(result["plan"]["goals"],[
            "巡回","投稿","キャンペーン","紹介","集計","メディア"])
        self.assertEqual(result["plan"]["not_connected_goals"],["キャンペーン","紹介"])
        self.assertEqual(result["plan"]["all_actions_count"],7)
        self.assertEqual(len(result["plan"]["recommendations"]),7)
        self.assertEqual(
            {item["action"] for item in result["plan"]["recommendations"]},
            {"tumblr.discover","tumblr.list_posts","tumblr.publish_draft",
             "engagement.like","engagement.reblog","engagement.follow",
             "analytics.get_summary"})
        self.assertTrue(all(
            item["confirmation_required"] for item in
            result["plan"]["recommendations"] if item["action"].startswith("engagement.")))
        self.assertTrue(all(item["basis"]=="既存Bridgeに登録された操作契約"
                            for item in result["plan"]["recommendations"]))
        self.assertEqual(adapter.calls,0)

    def test_goal_text_with_no_provider_must_plan_before_execution(self):
        adapter=RecordingAdapter()
        assistant=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[])
        result=assistant.turn("検索して 対象: tag:travel",goals=["巡回","集計"])
        self.assertEqual(result["state"],"guidance")
        self.assertFalse(result["executed"])
        self.assertEqual(result["plan"]["goals"],["巡回","集計"])
        self.assertEqual(adapter.calls,0)
        selected=assistant.turn(selected_action="tumblr.discover",
                                selected_target="tag:travel")
        self.assertEqual(selected["state"],"completed")
        self.assertEqual(adapter.calls,1)

    def test_owner_selected_actions_enforce_confirmation_and_allowlist(self):
        adapter=RecordingAdapter()
        assistant=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[])
        invalid=assistant.turn(selected_action="tumblr.delete_blog",selected_target="demo")
        self.assertEqual(invalid["state"],"invalid_tool")
        self.assertEqual(adapter.calls,0)
        proposed=assistant.turn(selected_action="tumblr.publish_draft",
                                selected_target="myblog")
        self.assertEqual(proposed["state"],"confirmation_required")
        self.assertEqual(adapter.calls,0)
        result=assistant.turn(confirm=True,request_id=proposed["request_id"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(adapter.calls,1)

    def test_unconfigured_goals_do_not_claim_an_action_is_implemented(self):
        assistant=Assistant(providers=[])
        result=assistant.turn(goals=["キャンペーン","紹介"])
        self.assertEqual(result["plan"]["recommendations"],[])
        self.assertEqual(result["plan"]["not_connected_goals"],
                         ["キャンペーン","紹介"])
        self.assertFalse(result["executed"])

    def test_invalid_goal_data_is_rejected_without_dispatch(self):
        adapter=RecordingAdapter()
        assistant=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[])
        for goals in (["巡回","巡回"],["made_up"],"巡回",[1],["投稿"]*7):
            with self.subTest(goals=goals):
                result=assistant.turn(goals=goals)
                self.assertEqual(result["state"],"invalid_goals")
        self.assertEqual(adapter.calls,0)

    def test_http_structured_goals_go_to_authenticated_assistant(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            release=build_release(ROOT/"app",Path(folder),SHA)
            adapter=RecordingAdapter()
            with AppServer(release,TOKEN,assistant=Assistant(
                    bridge=Bridge({"tumblr":adapter}),providers=[])) as server:
                url=f"http://127.0.0.1:{server.port}/assistant"
                status,response=post(url,TOKEN,{"message":"","goals":["巡回","集計"]})
                self.assertEqual(status,200)
                self.assertEqual(response["plan"]["goals"],["巡回","集計"])
                self.assertFalse(response["executed"])
                self.assertEqual(adapter.calls,0)
                status,response=post(url,TOKEN,{"selected_action":"tumblr.discover",
                                                  "selected_target":"tag:travel"})
                self.assertEqual(status,200)
                self.assertEqual(response["state"],"completed")
                self.assertEqual(adapter.calls,1)

    def test_chat_does_not_require_a_module_and_does_not_execute(self):
        result = Assistant(providers=[]).turn("募集の状況を分析して 対象 campaign-1")
        self.assertFalse(result["executed"])
        self.assertEqual("not_connected", result["state"])
        self.assertIn("実行していません", result["text"])

    def test_unconnected_publish_refuses_false_confirmation(self):
        assistant = Assistant(providers=[])
        preview = assistant.turn("下書きを公開して 対象 recruit-blog")
        self.assertEqual("not_connected", preview["state"])
        self.assertFalse(preview["executed"])
        self.assertIn("未連携", preview["text"])
        self.assertIsNone(preview["request_id"])

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

    def test_rate_limit_stops_without_consuming_fallback(self):
        paid = ScriptedProvider("groq", error="rate_limited")
        other_paid = ScriptedProvider("gemini", {"action": "analytics.get_summary", "target": "week"})
        free = ScriptedProvider("openrouter", {"action": "analytics.get_summary", "target": "week"}, paid=False)
        result = Assistant(providers=[paid, other_paid, free]).turn("短文")
        self.assertEqual("rate_limited", result["state"])
        self.assertFalse(result["executed"])
        self.assertEqual(1, len(paid.prompts))
        self.assertEqual([], other_paid.prompts)
        self.assertEqual(0, len(free.prompts))

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

    def test_ai_guides_without_a_tool_or_module_selection(self):
        provider = ScriptedProvider("gemini", {
            "action": "", "target": "",
            "reply": "まず募集投稿の目的と対象読者を整理しましょう。",
            "question": "どの層に届けたいですか？",
        })
        result = Assistant(providers=[provider]).turn("今週のTumblr運用を相談したい")
        self.assertEqual("guidance", result["state"])
        self.assertFalse(result["executed"])
        self.assertEqual("gemini", result["provider"])
        self.assertTrue(result["recovery"])
        self.assertIn("目的と対象読者", result["text"])
        self.assertIn("どの層", result["text"])

    def test_ai_guidance_does_not_call_adapter(self):
        adapter = RecordingAdapter()
        provider = ScriptedProvider("gemini", {
            "action": "", "reply": "下書きの構成案は導入、本文、誘導文です。",
        })
        result = Assistant(bridge=Bridge({"tumblr": adapter}), providers=[provider]).turn("投稿の文章を考えて")
        self.assertEqual("guidance", result["state"])
        self.assertFalse(result["executed"])
        self.assertEqual(0, adapter.calls)

    def test_ai_empty_reply_asks_for_details_instead_of_guessing(self):
        result = Assistant(providers=[ScriptedProvider("gemini", {})]).turn("よろしく")
        self.assertEqual("unclear", result["state"])
        self.assertFalse(result["executed"])
        self.assertTrue(result["recovery"])

    def test_ai_reply_wrong_type_is_rejected(self):
        result = Assistant(providers=[ScriptedProvider("gemini", {"reply": ["unexpected"]})]).turn("相談")
        self.assertEqual("invalid_response", result["state"])
        self.assertFalse(result["executed"])

    def test_http_assistant_guidance_without_module_selection(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            release = build_release(ROOT / "app", Path(folder), SHA)
            assistant = Assistant(providers=[ScriptedProvider("gemini", {
                "action": "", "target": "", "reply": "募集文の構成を提案します。",
                "question": "告知対象を教えてください。",
            })])
            with AppServer(release, TOKEN, assistant=assistant) as server:
                status, body = post(f"http://127.0.0.1:{server.port}/assistant", TOKEN, {
                    "message": "投稿案を相談したい",
                })
        self.assertEqual(200, status)
        self.assertEqual("guidance", body["state"])
        self.assertFalse(body["executed"])
        self.assertIn("募集文の構成", body["text"])
        self.assertIn("告知対象", body["text"])

    def test_http_assistant_without_auth_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            release = build_release(ROOT / "app", Path(folder), SHA)
            with AppServer(release, TOKEN, assistant=Assistant(providers=[])) as server:
                status, _ = post(f"http://127.0.0.1:{server.port}/assistant", "invalid", {
                    "message": "下書き案を作って",
                })
        self.assertEqual(401, status)


if __name__ == "__main__":
    unittest.main()
