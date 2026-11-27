"""The 79 catalog operations are selected inside the assistant conversation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tumblrkit.action_catalog import BRIDGE_ROUTES, BY_ID, CATALOG
from tumblrkit.assistant import Assistant
from tumblrkit.bridge import CONFIRM, Bridge
from tumblrkit.conversation import FIELDS, LOCAL_CONFIRM, blank_dialog, find_actions
from tumblrkit.conversation_ops import HANDLERS, perform
from tumblrkit.drafts import DraftStore


SAMPLES = {
    "text": "本文: あいさつ",
    "expected_text": "現在の本文: 元の文",
    "draft_id": "下書きID: " + ("ab" * 16),
    "target": "対象: demo",
    "query": "検索語: cat",
    "candidate_id": "候補ID: blog:1",
    "comment": "コメント: 見た",
    "engagement_action": "取消操作: like",
    "campaign_id": "企画ID: launch",
    "prize": "景品: 本",
    "deadline": "締切: 2000000000",
    "winners": "当選人数: 1",
    "claim_seconds": "受取秒数: 86400",
    "gate_topic_id": "GATE話題: なし",
    "event_id": "通知ID: event-1",
    "referrer_id": "紹介者: user-1",
    "source_post_id": "投稿ID: post-1",
    "destination": "共有先: なし",
    "name": "名前: 紹介企画",
    "member_id": "メンバー: member-1",
    "threshold": "段階人数: 3",
    "reward": "特典: 壁紙",
    "period": "期間: week",
    "publish_at": "予約時刻: 2000000000",
    "schedule_id": "予約ID: sched-1",
    "media_index": "メディア番号: 0",
    "rights": "権利: 承諾",
    "mode": "登録解除: 登録",
    "attachment_ids": "添付ID: なし",
    "media_job_ids": "動画ジョブ: なし",
    "apply": "適用タグ: なし",
}


def utterance(action_id):
    return "\n".join([BY_ID[action_id].name, *(SAMPLES[field] for field in FIELDS[action_id])])


class Probe:
    def __init__(self):
        self.seen = []
        self.publisher = SimpleNamespace(
            drafts=SimpleNamespace(get=lambda target: {"text": "固定本文"}),
            lifecycle=SimpleNamespace(preview=lambda action, target: {
                "approval": "ab" * 32, "post_id": "post-1",
                "current_text": "now", "new_text": "next",
            }),
        )

    def perform(self, action, target, payload):
        self.seen.append((action, target))
        return {"verified": True, "rows": []}

    def preview(self, action, target):
        return {"blog": "demo", "title": "item"}


class ConversationTests(unittest.TestCase):
    def test_every_official_name_selects_its_own_operation(self):
        self.assertEqual(len(FIELDS), 79)
        self.assertEqual(set(FIELDS), {item.action_id for item in CATALOG})
        for item in CATALOG:
            with self.subTest(action=item.action_id):
                self.assertEqual(find_actions(item.name), [item.action_id])

    def test_every_operation_can_be_used_from_one_conversation(self):
        owner = []

        def catalog_perform(action, payload):
            owner.append(action)
            return {"verified": True, "summary": action}

        probe = Probe()
        assistant = Assistant(
            bridge=Bridge({name: probe for name in ("tumblr", "engagement", "analytics", "telegram")}),
            providers=[], catalog_perform=catalog_perform)
        for item in CATALOG:
            with self.subTest(action=item.action_id):
                owner.clear()
                probe.seen.clear()
                assistant.dialog = blank_dialog()
                assistant.pending.clear()
                result = assistant.turn(utterance(item.action_id))
                bridged = BRIDGE_ROUTES.get(item.action_id)
                needs_confirm = item.action_id in LOCAL_CONFIRM or (bridged and bridged[0] in CONFIRM)
                if needs_confirm:
                    self.assertEqual(result["state"], "confirmation_required", result["text"])
                    self.assertNotIn(item.action_id, owner)
                    self.assertEqual(probe.seen, [])
                    result = assistant.turn(confirm=True, request_id=result["request_id"])
                self.assertEqual(result["state"], "completed", result["text"])
                if bridged:
                    self.assertTrue(probe.seen, item.action_id)
                    self.assertNotIn(item.action_id, owner)
                else:
                    self.assertEqual(owner, [item.action_id])
                    self.assertEqual(probe.seen, [])

    def test_missing_field_is_asked_then_the_next_sentence_executes(self):
        seen = []

        def catalog_perform(action, payload):
            seen.append(payload["text"])
            return {"verified": True, "summary": payload["text"], "published": False}

        assistant = Assistant(providers=[], catalog_perform=catalog_perform)
        asked = assistant.turn("投稿文をゼロから作る")
        self.assertEqual(asked["state"], "guidance")
        self.assertIn("本文", asked["text"])
        self.assertEqual(seen, [])
        done = assistant.turn("こんにちは")
        self.assertEqual(done["state"], "completed")
        self.assertIn("こんにちは", done["text"])
        self.assertEqual(seen, ["こんにちは"])
        self.assertFalse(done["executed"])

    def test_two_named_operations_run_in_one_reply(self):
        owner = []

        def catalog_perform(action, payload):
            owner.append(action)
            return {"verified": True, "summary": "流入を確認しました。"}

        probe = Probe()
        assistant = Assistant(
            bridge=Bridge({"analytics": probe}), providers=[], catalog_perform=catalog_perform)
        result = assistant.turn("今日の集計。未分類流入を見る")
        self.assertEqual(result["state"], "completed")
        self.assertIn("analytics.get_summary", [action for action, _ in probe.seen])
        self.assertEqual(probe.seen[0][1], "today")
        self.assertEqual(owner, ["analytics.10"])
        self.assertIn("流入を確認しました", result["text"])

    def test_plain_patrol_request_asks_what_to_scan_then_runs_it(self):
        probe = Probe()
        assistant = Assistant(bridge=Bridge({"tumblr": probe}), providers=[])
        asked = assistant.turn("巡回をしろ", goals=["巡回"])
        self.assertEqual(asked["state"], "guidance")
        self.assertIn("フォロワー", asked["text"])
        self.assertIn("新着", asked["text"])
        self.assertNotIn("操作名", asked["text"])
        self.assertEqual(probe.seen, [])
        done = assistant.turn("フォロワー")
        self.assertEqual(done["state"], "completed")
        self.assertEqual(probe.seen, [("tumblr.discover", "followers")])
        self.assertTrue(done["executed"])

    def test_summary_request_runs_today_without_an_official_name(self):
        probe = Probe()
        assistant = Assistant(bridge=Bridge({"analytics": probe}), providers=[])
        result = assistant.turn("集計して", goals=["集計"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(probe.seen, [("analytics.get_summary", "today")])

    def test_new_posts_request_asks_for_a_tag_then_scans_it(self):
        probe = Probe()

        def catalog_perform(action, payload):
            return perform(action, payload, {"tumblr": probe})

        assistant = Assistant(bridge=Bridge({"tumblr": probe}), providers=[],
                              catalog_perform=catalog_perform)
        asked = assistant.turn("巡回をしろ")
        self.assertIn("新着", asked["text"])
        narrowed = assistant.turn("新着")
        self.assertIn("タグ", narrowed["text"])
        self.assertEqual(probe.seen, [])
        done = assistant.turn("cats")
        self.assertEqual(done["state"], "completed")
        self.assertEqual(probe.seen, [("tumblr.discover", "tag:cats")])

    def test_registered_hashtag_patrol_checks_the_store_before_any_follow(self):
        def catalog_perform(action, payload):
            self.assertEqual(action, "discovery.registered_tags")
            return {"verified": True, "tags": [], "summary": "なし"}

        assistant = Assistant(providers=[], catalog_perform=catalog_perform)
        result = assistant.turn("登録されている#を巡回してフォロしてこい", goals=["巡回", "メディア"])
        self.assertFalse(result["executed"])
        self.assertIn("0件", result["text"])
        self.assertNotIn("操作名", result["text"])
        self.assertIn("タグ", result["text"])

    def test_registered_hashtag_patrol_continues_across_authenticated_turns(self):
        probe = Probe()

        def catalog_perform(action, payload):
            if action == "discovery.registered_tags":
                return {"verified": True, "tags": [], "summary": "なし"}
            return perform(action, payload, {"tumblr": probe})

        assistant = Assistant(bridge=Bridge({"tumblr": probe}), providers=[],
                              catalog_perform=catalog_perform)
        first = assistant.turn(
            "登録されている#を巡回してフォロしてこい", goals=["巡回", "メディア"],
            session_id="owner-a")
        self.assertEqual(first["state"], "input_required")
        self.assertIn("0件", first["text"])
        self.assertNotIn("操作名", first["text"])
        self.assertTrue(first.get("request_id"))
        self.assertEqual(probe.seen, [])
        second = assistant.turn(
            "cats", goals=["巡回", "メディア"], session_id="owner-a",
            request_id=first["request_id"])
        self.assertEqual(probe.seen, [("tumblr.discover", "tag:cats")])
        self.assertIn("フォロー", second["text"])
        self.assertNotIn(("engagement.follow", "demo"), probe.seen)

    def test_registered_hashtag_with_a_saved_tag_scans_then_asks_before_follow(self):
        probe = Probe()

        def catalog_perform(action, payload):
            if action == "discovery.registered_tags":
                return {"verified": True, "tags": ["cats"], "summary": "登録済みタグ: cats。"}
            raise AssertionError(action)

        def perform(self, action, target, payload):
            self.seen.append((action, target))
            return {"verified": True, "rows": [{
                "blog": "demo", "title": "demo", "type": "blog", "date": "",
                "thumbnail_url": "", "url": "https://demo.tumblr.com",
            }]}

        probe.perform = perform.__get__(probe, Probe)
        assistant = Assistant(
            bridge=Bridge({"tumblr": probe, "engagement": probe}),
            providers=[], catalog_perform=catalog_perform)
        result = assistant.turn("登録されている#を巡回してフォロしてこい")
        self.assertEqual(probe.seen, [("tumblr.discover", "tag:cats")])
        self.assertEqual(result["state"], "confirmation_required")
        self.assertIn("フォロー", result["text"])
        self.assertFalse(result["executed"])

    def test_delete_by_its_official_name_asks_instead_of_refusing(self):
        result = Assistant(providers=[]).turn("投稿を削除")
        self.assertEqual(result["state"], "guidance")
        self.assertIn("対象", result["text"])
        refused = Assistant(providers=[]).turn("ブログを削除して")
        self.assertEqual(refused["state"], "refused_unlisted")

    def test_local_handlers_cover_every_non_bridge_operation(self):
        local = {item.action_id for item in CATALOG if item.action_id not in BRIDGE_ROUTES}
        self.assertEqual(set(HANDLERS), local)
        self.assertEqual(len(local), 65)
        for action in sorted(local):
            with self.subTest(action=action):
                with self.assertRaises(ValueError):
                    perform(action, {}, {})

    def test_new_draft_text_is_saved_by_the_existing_store(self):
        with tempfile.TemporaryDirectory() as folder:
            store = DraftStore(Path(folder) / "drafts.sqlite3")
            result = perform("publisher.01", {"text": "保存する文"}, {"drafts": store})
            self.assertTrue(result["verified"])
            self.assertFalse(result["published"])
            self.assertIn("保存する文", store.preview(result["draft_id"])["text"])
            self.assertIn("下書きID", result["summary"])


if __name__ == "__main__":
    unittest.main()
