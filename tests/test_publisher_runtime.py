"""Offline Publisher→Bridge→draft→official NPF→readback acceptance.

All official HTTP calls are replaced by explicitly local deterministic fakes.
No real post is created by these tests.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tumblrkit.drafts import DraftStore
from tumblrkit.publisher_runtime import PublisherAdapter
from tumblrkit.tumblr_read import TumblrReadAdapter
from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge


class OfflineBudget:
    def __init__(self):
        self.costs=[]
    def reserve(self,cost):
        self.costs.append(cost)
    def pause_after_limit(self):
        pass


class OfflinePost:
    def __init__(self,blog="owner",post_id="23456789"):
        self.blog=blog
        self.id=post_id
        self.posts=[]
        self.mismatch=False
        self.failed=None
    def post_json(self,path,bearer,body,budget):
        if self.failed:raise ValueError(self.failed)
        self.posts.append((path,bearer,body))
        return {"meta":{"status":201},"response":{"id":self.id}}
    def get(self,path,bearer):
        assert bearer=="sample-token"
        assert "/posts/"+self.id in path
        return {"meta":{"status":200},"response":{
            "id_string":self.id,
            "blog_name":"wrong" if self.mismatch else self.blog,
            "state":"published",
            "date":"2026-11-11",
            "content":[{"type":"text","text":self.posts[0][2]["content"][0]["text"]}],
        }}


class PublisherRuntimeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder=Path(tmp.name)
        os.chmod(self.folder,0o700)
        self.drafts=DraftStore(self.folder/"drafts.sqlite3")
        self.draft=self.drafts.save("本人確認済みの募集文。")
        self.budget=OfflineBudget()
        self.provider=OfflinePost()
        self.publisher=PublisherAdapter("owner",self.drafts,
            lambda:"sample-token",read_transport=self.provider,
            write_transport=self.provider,budget=self.budget)

    def test_posted_once_after_exact_content_confirmation_then_readback(self):
        reader=TumblrReadAdapter("owner",self.folder/"access",
            transport=self.provider,token_provider=lambda:"sample-token")
        reader.publisher=self.publisher
        chat=Assistant(bridge=Bridge({"tumblr":reader}),providers=[])
        proposal=chat.turn(selected_action="tumblr.publish_draft",
                           selected_target=self.draft["id"])
        self.assertEqual(proposal["state"],"confirmation_required")
        self.assertIn("本人確認済みの募集文。",proposal["text"])
        self.assertEqual(self.provider.posts,[])
        finished=chat.turn(confirm=True,request_id=proposal["request_id"])
        self.assertEqual(finished["state"],"completed")
        self.assertTrue(finished["executed"])
        self.assertEqual(finished["view"]["rows"][0]["status"],"published")
        self.assertEqual(len(self.provider.posts),1)
        path,token,body=self.provider.posts[0]
        self.assertEqual(path,"/v2/blog/owner.tumblr.com/posts")
        self.assertEqual(token,"sample-token")
        self.assertEqual(body,{"state":"published",
            "content":[{"type":"text","text":"本人確認済みの募集文。"}],
            "send_to_twitter":False})
        self.assertEqual(self.budget.costs,[2])
        again=self.publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.assertTrue(again["verified"])
        self.assertEqual(len(self.provider.posts),1)
        self.assertEqual(self.budget.costs,[2,1])
        with sqlite3.connect(self.drafts.path) as db:
            self.assertEqual(db.execute(
                "SELECT state FROM tumblr_publish_receipts WHERE draft_id=?",
                (self.draft["id"],)).fetchone()[0],"verified")

    def test_changed_draft_between_preview_and_confirm_cannot_publish(self):
        reader=TumblrReadAdapter("owner",self.folder/"access",
            transport=self.provider,token_provider=lambda:"sample-token")
        reader.publisher=self.publisher
        chat=Assistant(bridge=Bridge({"tumblr":reader}),providers=[])
        before=chat.turn(selected_action="tumblr.publish_draft",
                         selected_target=self.draft["id"])
        self.assertEqual(before["state"],"confirmation_required")
        self.assertIn("本人確認済みの募集文。",before["text"])
        self.drafts.update(self.draft["id"],"全く違う新しい本文",
                           "本人確認済みの募集文。")
        denied=chat.turn(confirm=True,request_id=before["request_id"])
        self.assertEqual(denied["state"],"failed")
        self.assertFalse(denied["executed"])
        self.assertIn("変更",denied["text"])
        self.assertEqual(self.provider.posts,[])
        self.assertEqual(self.budget.costs,[])
        # A fresh preview of the changed text can be confirmed normally.
        next_preview=chat.turn(selected_action="tumblr.publish_draft",
                                selected_target=self.draft["id"])
        self.assertIn("全く違う新しい本文",next_preview["text"])
        approved=chat.turn(confirm=True,request_id=next_preview["request_id"])
        self.assertEqual(approved["state"],"completed")
        self.assertEqual(len(self.provider.posts),1)

    def test_deleted_draft_between_preview_and_confirm_does_not_publish(self):
        reader=TumblrReadAdapter("owner",self.folder/"access",
            transport=self.provider,token_provider=lambda:"sample-token")
        reader.publisher=self.publisher
        chat=Assistant(bridge=Bridge({"tumblr":reader}),providers=[])
        preview=chat.turn(selected_action="tumblr.publish_draft",
                          selected_target=self.draft["id"])
        self.assertTrue(self.drafts.delete(self.draft["id"]))
        denied=chat.turn(confirm=True,request_id=preview["request_id"])
        self.assertEqual(denied["state"],"failed")
        self.assertFalse(denied["executed"])
        self.assertEqual(self.provider.posts,[])

    def test_numeric_receipt_and_nested_blog_identity_are_supported(self):
        class NumericProvider(OfflinePost):
            def post_json(self,path,bearer,body,budget):
                self.posts.append((path,bearer,body))
                return {"meta":{"status":201},"response":{"id":23456789}}
            def get(self,path,bearer):
                return {"meta":{"status":200},"response":{
                    "object_type":"post","type":"blocks",
                    "id":23456789,"blog":{"name":"owner"},"state":"published",
                    "content":[{"type":"text",
                                "text":self.posts[0][2]["content"][0]["text"]}]}}
        provider=NumericProvider()
        publisher=PublisherAdapter("owner",self.drafts,lambda:"sample-token",
            read_transport=provider,write_transport=provider,budget=self.budget)
        result=publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.assertTrue(result["verified"])
        self.assertEqual(result["provider_post_id"],"23456789")
        self.assertEqual(len(provider.posts),1)

    def test_unknown_result_blocks_duplicate_even_across_instance_restart(self):
        self.provider.failed="tumblr_publish_uncertain"
        with self.assertRaisesRegex(ValueError,"tumblr_publish_uncertain"):
            self.publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.provider.failed=None
        restored=PublisherAdapter("owner",DraftStore(self.drafts.path),
             lambda:"sample-token",read_transport=self.provider,
             write_transport=self.provider,budget=self.budget)
        with self.assertRaisesRegex(ValueError,"tumblr_publish_reconcile_required"):
            restored.perform("tumblr.publish_draft",self.draft["id"],{})
        self.assertEqual(self.provider.posts,[])

    def test_known_provider_refusal_can_be_explicitly_retried(self):
        self.provider.failed="tumblr_permission_denied"
        with self.assertRaisesRegex(ValueError,"tumblr_permission_denied"):
            self.publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.provider.failed=None
        result=self.publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.assertTrue(result["verified"])
        self.assertEqual(len(self.provider.posts),1)

    def test_failed_readback_never_reposts_and_can_reconcile(self):
        self.provider.mismatch=True
        with self.assertRaisesRegex(ValueError,"tumblr_publish_readback_unverified"):
            self.publisher.perform("tumblr.publish_draft",self.draft["id"],{})
        self.assertEqual(len(self.provider.posts),1)
        self.provider.mismatch=False
        self.assertTrue(self.publisher.perform(
            "tumblr.publish_draft",self.draft["id"],{})["verified"])
        self.assertEqual(len(self.provider.posts),1)

    def test_no_unverified_target_payload_or_budget_leaves_no_post(self):
        with self.assertRaises(ValueError):
            self.publisher.perform("tumblr.publish_draft","invalid",{})
        with self.assertRaises(ValueError):
            self.publisher.perform("tumblr.publish_draft",self.draft["id"],
                                    {"state":"published"})
        with self.assertRaisesRegex(ValueError,"tumblr_shared_api_budget_required"):
            PublisherAdapter("owner",self.drafts,lambda:"sample-token")
        self.assertEqual(self.provider.posts,[])


if __name__=="__main__":
    unittest.main()
