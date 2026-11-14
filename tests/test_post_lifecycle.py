"""Offline acceptance for the *existing* owner's post edit/delete route.

No live Tumblr request. The fake simulates the official create, NPF PUT,
legacy delete POST and OAuth readback shapes.
"""
import json
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


class Budget:
    def __init__(self):self.costs=[]
    def reserve(self, amount):self.costs.append(amount)
    def pause_after_limit(self):pass


class OfficialFake:
    def __init__(self):
        self.text=None
        self.deleted=False
        self.post_id="12345678987654321"
        self.calls=[]
        self.fail_next=None
        self.remote_change=False

    def post_json(self,path,bearer,body,budget):
        assert bearer=="test-owner-token"
        assert path=="/v2/blog/owner.tumblr.com/posts"
        self.calls.append(("create",body))
        self.text=body["content"][0]["text"]
        return {"meta":{"status":201},"response":{"id":self.post_id}}

    def get(self,path,bearer):
        assert bearer=="test-owner-token"
        assert path.endswith("/posts/"+self.post_id+"?post_format=npf")
        self.calls.append(("get",path))
        if self.deleted:
            raise ValueError("tumblr_post_unavailable")
        return {"meta":{"status":200},"response":{
            "id_string":self.post_id,"blog_name":"owner","state":"published",
            "content":[{"type":"text","text":(
                "外部の未確認変更" if self.remote_change else self.text)}],
        }}

    def mutate_json(self,method,path,bearer,body,budget):
        assert bearer=="test-owner-token"
        if self.fail_next:
            failure=self.fail_next
            self.fail_next=None
            raise ValueError(failure)
        if method=="PUT":
            assert path=="/v2/blog/owner.tumblr.com/posts/"+self.post_id
            assert body["state"]=="published"
            self.calls.append(("edit",body))
            self.text=body["content"][0]["text"]
            return {"meta":{"status":200},"response":{"id":self.post_id}}
        assert method=="POST"
        assert path=="/v2/blog/owner.tumblr.com/post/delete"
        assert body["id"]==self.post_id
        self.calls.append(("delete",body))
        self.deleted=True
        return {"meta":{"status":200},"response":{}}


class PublishedLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder=Path(temporary.name)
        os.chmod(folder,0o700)
        self.store=DraftStore(folder/"drafts.sqlite3")
        self.budget=Budget()
        self.fake=OfficialFake()
        self.publisher=PublisherAdapter(
            "owner",self.store,lambda:"test-owner-token",
            read_transport=self.fake,write_transport=self.fake,
            budget=self.budget)
        self.reader=TumblrReadAdapter(
            "owner",folder/"token",transport=self.fake,
            token_provider=lambda:"test-owner-token")
        self.reader.publisher=self.publisher
        self.assistant=Assistant(bridge=Bridge({"tumblr":self.reader}),providers=[])
        self.source=self.store.save("一度公開した投稿")
        published=self.publisher.perform("tumblr.publish_draft",self.source["id"],{})
        self.assertTrue(published["verified"])
        self.fake.calls.clear()
        self.budget.costs.clear()

    def _edit(self,new_text):
        revision=self.store.save(new_text)
        return self.source["id"]+":"+revision["id"],revision

    def _propose(self,action,target):
        out=self.assistant.turn(selected_action=action,selected_target=target)
        self.assertEqual(out["state"],"confirmation_required",out)
        self.assertFalse(out["executed"])
        return out

    def test_edit_uses_exact_before_after_preview_and_official_put_once(self):
        target,revision=self._edit("本人が修正した新しい全文")
        proposal=self._propose("tumblr.edit_post",target)
        self.assertIn("一度公開した投稿",proposal["text"])
        self.assertIn("本人が修正した新しい全文",proposal["text"])
        self.assertFalse(any(call[0]=="edit" for call in self.fake.calls))
        result=self.assistant.turn(confirm=True,request_id=proposal["request_id"])
        self.assertEqual(result["state"],"completed",result)
        self.assertTrue(result["executed"])
        self.assertEqual(result["view"]["rows"][0]["status"],"edited")
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="edit"]),1)
        self.assertEqual(self.budget.costs,[3])
        listing=self.store.list()
        source=next(x for x in listing if x["id"]==self.source["id"])
        self.assertEqual(source["publish_state"],"published")
        self.assertEqual(source["current_text"],"本人が修正した新しい全文")
        self.assertEqual(source["provider_post_id"],self.fake.post_id)
        rev=next(x for x in listing if x["id"]==revision["id"])
        self.assertTrue(rev["locked"])
        with self.assertRaisesRegex(ValueError,"DRAFT_PUBLISH_LOCKED"):
            self.store.update(revision["id"],"不許可の改変",revision["text"])
        with self.assertRaisesRegex(ValueError,"DRAFT_PUBLISH_LOCKED"):
            self.store.delete(revision["id"])

    def test_multiple_owner_approved_revisions_never_create_new_post(self):
        first,_=self._edit("一回目の修正")
        req=self._propose("tumblr.edit_post",first)
        self.assertEqual(self.assistant.turn(
            confirm=True,request_id=req["request_id"])["state"],"completed")
        second,_=self._edit("二回目の修正")
        req=self._propose("tumblr.edit_post",second)
        self.assertIn("一回目の修正",req["text"])
        self.assertEqual(self.assistant.turn(
            confirm=True,request_id=req["request_id"])["state"],"completed")
        self.assertEqual(self.fake.text,"二回目の修正")
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="edit"]),2)
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="create"]),0)

    def test_stale_edit_preview_refuses_after_revision_mutation(self):
        target,revision=self._edit("第一の修正")
        req=self._propose("tumblr.edit_post",target)
        self.store.update(revision["id"],"あとから変更","第一の修正")
        failure=self.assistant.turn(confirm=True,request_id=req["request_id"])
        self.assertEqual(failure["state"],"failed")
        self.assertFalse(failure["executed"])
        self.assertEqual(len(self.fake.calls),0)

    def test_delete_only_from_verified_owner_receipt_and_confirmed(self):
        proposal=self._propose("tumblr.delete_post",self.source["id"])
        self.assertIn("取り消せない",proposal["text"])
        self.assertFalse(self.fake.deleted)
        result=self.assistant.turn(confirm=True,request_id=proposal["request_id"])
        self.assertEqual(result["state"],"completed",result)
        self.assertEqual(result["view"]["rows"][0]["status"],"deleted")
        self.assertTrue(self.fake.deleted)
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="delete"]),1)
        listing=self.store.list()
        source=next(x for x in listing if x["id"]==self.source["id"])
        self.assertEqual(source["publish_state"],"deleted")
        target,_=self._edit("編集できない")
        with self.assertRaisesRegex(ValueError,"tumblr_post_already_deleted"):
            self.publisher.lifecycle.preview("tumblr.edit_post",target)

    def test_owner_guard_refuses_arbitrary_post_id_and_unpublished_draft(self):
        revision=self.store.save("公開していないローカル原稿")
        for action,target in (
            ("tumblr.delete_post",self.fake.post_id),
            ("tumblr.delete_post",revision["id"]),
            ("tumblr.edit_post",self.source["id"]+":"+self.source["id"]),
            ("tumblr.edit_post",self.source["id"]+":bad"),
        ):
            with self.subTest(action=action,target=target):
                invalid=self.assistant.turn(selected_action=action,selected_target=target)
                self.assertFalse(invalid["executed"])
                self.assertNotEqual(invalid["state"],"confirmation_required")
        self.assertFalse(self.fake.deleted)
        self.assertEqual(len(self.fake.calls),0)

    def test_unknown_provider_result_is_not_retried_after_restart(self):
        target,_=self._edit("不確かな通信")
        preview=self.publisher.lifecycle.preview("tumblr.edit_post",target)
        self.fake.fail_next="tumblr_mutation_uncertain"
        with self.assertRaisesRegex(ValueError,"tumblr_mutation_uncertain"):
            self.publisher.lifecycle.perform("tumblr.edit_post",target,
                                      {"expected_digest":preview["approval"]})
        new_publisher=PublisherAdapter("owner",DraftStore(self.store.path),
            lambda:"test-owner-token",read_transport=self.fake,
            write_transport=self.fake,budget=self.budget)
        with self.assertRaisesRegex(ValueError,"tumblr_post_reconcile_required"):
            new_publisher.lifecycle.perform("tumblr.edit_post",target,
                                      {"expected_digest":preview["approval"]})
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="edit"]),0)

    def test_explicit_reconcile_unknown_edit_reads_provider_without_repost(self):
        target,revision=self._edit("編集結果が一時不明")
        approved=self.publisher.lifecycle.preview("tumblr.edit_post",target)
        self.fake.fail_next="tumblr_mutation_uncertain"
        with self.assertRaisesRegex(ValueError,"tumblr_mutation_uncertain"):
            self.publisher.lifecycle.perform("tumblr.edit_post",target,
                                             {"expected_digest":approved["approval"]})
        pending=self.store.list()
        source=next(x for x in pending if x["id"]==self.source["id"])
        self.assertEqual(source["publish_state"],"changing")
        # Initial GET still sees the old text, so no fabricated verification.
        result=self.assistant.turn(selected_action="tumblr.reconcile_post",
                                   selected_target=self.source["id"])
        self.assertNotEqual(result["state"],"completed")
        self.assertFalse(result["executed"])
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="edit"]),0)
        # Real Tumblr may have accepted the mutation before network failure.
        self.fake.text="編集結果が一時不明"
        restored=PublisherAdapter("owner",DraftStore(self.store.path),
            lambda:"test-owner-token",read_transport=self.fake,
            write_transport=self.fake,budget=self.budget)
        reader=TumblrReadAdapter("owner",self.store.path.parent/"token",
            transport=self.fake,token_provider=lambda:"test-owner-token")
        reader.publisher=restored
        assistant=Assistant(bridge=Bridge({"tumblr":reader}),providers=[])
        verified=assistant.turn(selected_action="tumblr.reconcile_post",
                                selected_target=self.source["id"])
        self.assertEqual(verified["state"],"completed")
        self.assertEqual(verified["view"]["rows"][0]["status"],"edited")
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="edit"]),0)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute(
                "SELECT state FROM tumblr_post_mutations "
                "WHERE operation_key=?",
                ("edit:"+revision["id"],)).fetchone()[0],"verified")

    def test_explicit_reconcile_unknown_delete_never_reissues_delete(self):
        approved=self.publisher.lifecycle.preview(
            "tumblr.delete_post",self.source["id"])
        self.fake.fail_next="tumblr_mutation_uncertain"
        with self.assertRaisesRegex(ValueError,"tumblr_mutation_uncertain"):
            self.publisher.lifecycle.perform("tumblr.delete_post",
                self.source["id"],{"expected_digest":approved["approval"]})
        denied=self.assistant.turn(selected_action="tumblr.reconcile_post",
                                  selected_target=self.source["id"])
        self.assertEqual(denied["state"],"failed")
        self.fake.deleted=True
        readback=self.assistant.turn(selected_action="tumblr.reconcile_post",
                                    selected_target=self.source["id"])
        self.assertEqual(readback["state"],"completed")
        self.assertEqual(readback["view"]["rows"][0]["status"],"deleted")
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="delete"]),0)
        current=next(x for x in self.store.list() if x["id"]==self.source["id"])
        self.assertEqual(current["publish_state"],"deleted")

    def test_malformed_owner_post_precheck_does_not_lock_or_edit(self):
        target,_=self._edit("編集してはならない")
        preview=self.publisher.lifecycle.preview("tumblr.edit_post",target)
        original=self.fake.get
        self.fake.get=lambda path,bearer: {"meta":None,"response":{}}
        with self.assertRaisesRegex(ValueError,"tumblr_post_external_changed"):
            self.publisher.lifecycle.perform("tumblr.edit_post",target,
                            {"expected_digest":preview["approval"]})
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="edit"]),0)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tumblr_post_mutations").fetchone()[0],0)
        self.fake.get=original
        verified=self.publisher.lifecycle.perform("tumblr.edit_post",target,
                            {"expected_digest":preview["approval"]})
        self.assertTrue(verified["verified"])
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="edit"]),1)

    def test_malformed_after_edit_readback_only_reconciles_no_repost(self):
        target,_=self._edit("新しい編集内容")
        preview=self.publisher.lifecycle.preview("tumblr.edit_post",target)
        original=self.fake.get
        def malformed_after_edit(path,bearer):
            if self.fake.text=="新しい編集内容":
                return {"meta":None,"response":{}}
            return original(path,bearer)
        self.fake.get=malformed_after_edit
        with self.assertRaisesRegex(ValueError,"tumblr_post_readback_unverified"):
            self.publisher.lifecycle.perform("tumblr.edit_post",target,
                            {"expected_digest":preview["approval"]})
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="edit"]),1)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute(
                "SELECT state FROM tumblr_post_mutations").fetchone()[0],"sent")
        self.fake.get=original
        result=self.publisher.lifecycle.reconcile(self.source["id"])
        self.assertTrue(result["verified"])
        self.assertEqual(len([c for c in self.fake.calls if c[0]=="edit"]),1)

    def test_external_change_blocks_update_and_delete_without_side_effects(self):
        target,_=self._edit("勝手に上書きさせない")
        preview=self.publisher.lifecycle.preview("tumblr.edit_post",target)
        self.fake.remote_change=True
        with self.assertRaisesRegex(ValueError,"tumblr_post_external_changed"):
            self.publisher.lifecycle.perform("tumblr.edit_post",target,
                                      {"expected_digest":preview["approval"]})
        self.assertEqual(len([x for x in self.fake.calls if x[0]=="edit"]),0)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tumblr_post_mutations").fetchone()[0],0)
        self.fake.remote_change=False
        deleted=self.publisher.lifecycle.preview(
            "tumblr.delete_post",self.source["id"])
        self.fake.remote_change=True
        with self.assertRaisesRegex(ValueError,"tumblr_post_external_changed"):
            self.publisher.lifecycle.perform("tumblr.delete_post",self.source["id"],
                                      {"expected_digest":deleted["approval"]})
        self.assertFalse(self.fake.deleted)


if __name__=="__main__":
    unittest.main()
