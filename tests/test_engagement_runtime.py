"""Full offline Engagement path: observed target -> owner preview -> consented
single official call -> provider readback -> durable at-most-once receipt.

No real social-media action or external network request occurs in these tests.
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.engagement_runtime import EngagementAdapter


class Budget:
    def __init__(self):
        self.calls=[]
    def reserve(self,count=2):
        self.calls.append(count)
    def pause_after_limit(self):
        pass


class Reader:
    def __init__(self):
        self.calls=[]
        self.liked=False
        self.followed=False
        self.reblog_parent="123"
        self.fail_readback=False

    def get(self,path,bearer):
        assert bearer=="valid-owner-token"
        self.calls.append(path)
        if "/user/following?" in path:
            return {"meta":{"status":200},"response":{
                "total_blogs":1 if self.followed else 0,
                "blogs":[{"name":"source"}] if self.followed else []}}
        if "/info" in path:
            return {"meta":{"status":200},"response":{"blog":{
                "name":"source","followed":self.followed}}}
        if "/source.tumblr.com/posts/123?" in path:
            post={"blog_name":"source","id_string":"123",
                  "reblog_key":"verified-key","liked":self.liked,
                  "state":"published"}
            if self.fail_readback and self.liked:
                post["id_string"]="999"
            return {"meta":{"status":200},"response":post}
        if "/owner.tumblr.com/posts/456?" in path:
            post={"blog_name":"owner","id_string":"456","parent_post_id":
                  self.reblog_parent,"state":"published"}
            return {"meta":{"status":200},"response":post}
        raise AssertionError("Unexpected external request")


class Writer:
    def __init__(self,reader):
        self.reader=reader
        self.calls=[]
        self.failure=None

    def post(self,path,bearer,params,budget):
        assert bearer=="valid-owner-token"
        self.calls.append((path,params))
        if self.failure:
            raise ValueError(self.failure)
        if path=="/v2/user/like":
            self.reader.liked=True
            return 200,{"meta":{"status":200},"response":{}}
        if path=="/v2/user/follow":
            self.reader.followed=True
            return 200,{"meta":{"status":200},"response":{}}
        if path=="/v2/blog/owner.tumblr.com/post/reblog":
            return 201,{"meta":{"status":201},"response":{"id":"456"}}
        raise AssertionError("Unsafe path")


class EngagementTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root=Path(tmp.name)
        os.chmod(root,0o700)
        self.store=DiscoveryStore(root/"discovery.sqlite3")
        self.store.save_page("tag:topic",
            [{"name":"source","url":"https://source.tumblr.com"}],
            [{"candidate_id":"source:123","blog":"source","post_id":"123",
              "url":"https://source.tumblr.com/post/123",
              "title":"actual observed post","type":"text",
              "date":"","thumbnail_url":"","text_excerpt":"",
              "tags":[],"note_count":0,"media_refs":[]}],
            1,complete=True)
        self.reader=Reader()
        self.writer=Writer(self.reader)
        self.budget=Budget()
        self.adapter=EngagementAdapter("owner",self.store,
            lambda:"valid-owner-token",self.reader,self.budget,self.writer)

    def bot(self):
        return Assistant(bridge=Bridge({"engagement":self.adapter}),providers=[])

    def test_like_requires_confirmation_real_candidate_and_readback(self):
        chat=self.bot()
        preview=chat.turn(selected_action="engagement.like",
                          selected_target="source:123")
        self.assertEqual(preview["state"],"confirmation_required")
        self.assertIn("actual observed post",preview["text"])
        self.assertEqual(self.writer.calls,[])
        done=chat.turn(confirm=True,request_id=preview["request_id"])
        self.assertEqual(done["state"],"completed")
        self.assertTrue(done["executed"])
        self.assertEqual(done["view"]["title"],"いいね確認結果")
        self.assertEqual(len(self.writer.calls),1)
        self.assertEqual(self.writer.calls[0][0],"/v2/user/like")
        self.assertEqual(self.writer.calls[0][1],
                         {"id":"123","reblog_key":"verified-key"})
        self.assertEqual(self.budget.calls,[3])
        self.assertEqual(len(self.reader.calls),2)
        # Repeated request after process restart never sends another Like.
        restarted=EngagementAdapter("owner",self.store,
            lambda:"valid-owner-token",self.reader,self.budget,self.writer)
        replay=restarted.perform("engagement.like","source:123",{})
        self.assertTrue(replay["verified"])
        self.assertTrue(replay["reused_existing_receipt"])
        self.assertEqual(len(self.writer.calls),1)

    def test_follow_selected_blog_with_explicit_authorization(self):
        chat=self.bot()
        preview=chat.turn(selected_action="engagement.follow",
                          selected_target="source")
        self.assertEqual(preview["state"],"confirmation_required")
        result=chat.turn(confirm=True,request_id=preview["request_id"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(self.writer.calls[0],
                         ("/v2/user/follow",
                          {"url":"https://source.tumblr.com"}))
        self.assertEqual(self.budget.calls,[3])
        self.assertEqual(len(self.reader.calls),2)

    def test_reblog_writes_only_to_preselected_owner_blog_and_verifies_parent(self):
        chat=self.bot()
        preview=chat.turn(selected_action="engagement.reblog",
                          selected_target="source:123")
        self.assertEqual(preview["state"],"confirmation_required")
        result=chat.turn(confirm=True,request_id=preview["request_id"])
        self.assertEqual(result["state"],"completed")
        self.assertEqual(self.writer.calls[0][0],
                         "/v2/blog/owner.tumblr.com/post/reblog")
        self.assertEqual(self.writer.calls[0][1],
                         {"id":"123","reblog_key":"verified-key"})
        self.assertEqual(result["view"]["rows"][0]["url"],
                         "https://owner.tumblr.com/post/456")
        self.assertEqual(self.budget.calls,[3])
        self.assertEqual(len(self.reader.calls),2)

    def test_unknown_post_reply_is_not_retried_after_restart(self):
        self.writer.failure="engagement_post_uncertain"
        chat=self.bot()
        preview=chat.turn(selected_action="engagement.like",
                          selected_target="source:123")
        outcome=chat.turn(confirm=True,request_id=preview["request_id"])
        self.assertEqual(outcome["state"],"failed")
        self.assertIn("自動再送しません",outcome["text"])
        self.writer.failure=None
        with self.assertRaisesRegex(ValueError,"engagement_reconcile_required"):
            EngagementAdapter("owner",self.store,lambda:"valid-owner-token",
                self.reader,self.budget,self.writer).perform(
                    "engagement.like","source:123",{})
        self.assertEqual(len(self.writer.calls),1)

    def test_uncertain_readback_keeps_sent_and_rechecks_without_reposting(self):
        self.reader.fail_readback=True
        with self.assertRaisesRegex(ValueError,"engagement_readback_unverified"):
            self.adapter.perform("engagement.like","source:123",{})
        self.assertEqual(len(self.writer.calls),1)
        with sqlite3.connect(self.store.path) as db:
            row=db.execute("SELECT state FROM engagement_receipts").fetchone()
        self.assertEqual(row[0],"sent")
        self.reader.fail_readback=False
        result=self.adapter.perform("engagement.like","source:123",{})
        self.assertTrue(result["verified"])
        self.assertEqual(self.budget.calls,[3,1])
        self.assertEqual(len(self.writer.calls),1)

    def test_excluded_unknown_or_self_target_does_not_touch_provider(self):
        for action,target in (("engagement.like","other:123"),
                              ("engagement.reblog","source:999"),
                              ("engagement.follow","owner"),
                              ("engagement.follow","notobserved"),
                              ("engagement.follow","https://evil.example")):
            with self.subTest(action=action,target=target):
                result=self.bot().turn(selected_action=action,selected_target=target)
                self.assertEqual(result["state"],"failed")
        self.store.exclude_blog("source")
        result=self.bot().turn(selected_action="engagement.like",
                               selected_target="source:123")
        self.assertEqual(result["state"],"failed")
        self.assertEqual(self.writer.calls,[])
        self.assertEqual(self.reader.calls,[])
        self.assertEqual(self.budget.calls,[])

    def test_write_permission_missing_prevents_all_post_calls(self):
        def no_write():
            raise ValueError("tumblr_write_consent_required")
        adapter=EngagementAdapter("owner",self.store,no_write,
                                 self.reader,self.budget,self.writer)
        chat=Assistant(bridge=Bridge({"engagement":adapter}),providers=[])
        proposal=chat.turn(selected_action="engagement.follow",
                           selected_target="source")
        result=chat.turn(confirm=True,request_id=proposal["request_id"])
        self.assertEqual(result["state"],"failed")
        self.assertIn("投稿権限",result["text"])
        self.assertEqual(self.writer.calls,[])
        self.assertEqual(self.reader.calls,[])

    def test_no_unlisted_action_or_batch_payload(self):
        with self.assertRaises(ValueError):
            self.adapter.perform("engagement.bulk_like","source:123",{})
        with self.assertRaises(ValueError):
            self.adapter.perform("engagement.like","source:123",
                                 {"targets":["source:123","source:124"]})
        self.assertEqual(self.writer.calls,[])


if __name__=="__main__":
    unittest.main()
