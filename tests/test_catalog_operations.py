"""Offline coverage for the 14 previously unrouted owner operations."""
import os
import tempfile
import time
import unittest
from pathlib import Path

from tumblrkit.analytics_runtime import AnalyticsAdapter
from tumblrkit.attachments import AttachmentStore
from tumblrkit.campaign_runtime import CampaignContest
from tumblrkit.catalog_operations import perform
from tumblrkit.core import connect, record_event
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.drafts import DraftStore
from tumblrkit.media_save import MediaSave


class CatalogOperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        os.chmod(root, 0o700)
        self.drafts = DraftStore(root / "drafts.sqlite3")
        self.discovery = DiscoveryStore(root / "discovery.sqlite3")
        self.discovery.save_page(
            "tag:topic",
            [{"name": "source", "url": "https://source.tumblr.com"}],
            [{"candidate_id": "source:123", "blog": "source", "post_id": "123",
              "url": "https://source.tumblr.com/post/123", "title": "observed",
              "type": "text", "date": "", "thumbnail_url": "", "text_excerpt": "art",
              "tags": ["art", "night"], "note_count": 0, "media_refs": []}],
            1, complete=True)
        self.db = connect(str(root / "ledger.sqlite3"))
        self.addCleanup(self.db.close)
        self.analytics = AnalyticsAdapter(self.discovery, campaign_db=self.db)
        self.attachments = AttachmentStore(root / "files")
        self.media = MediaSave(self.discovery, root / "media")

    def test_publisher_compose_revise_reuse_attach_and_tags_stay_local(self):
        created = perform("publisher.01", {"text": "art night draft"}, drafts=self.drafts)
        self.assertFalse(created["published"])
        revised = perform("publisher.02", {
            "draft_id": created["draft_id"], "text": "art night revised",
            "expected_text": created["text"]}, drafts=self.drafts)
        self.assertEqual(revised["text"], "art night revised")
        reused = perform("publisher.03", {"draft_id": revised["draft_id"]}, drafts=self.drafts)
        self.assertNotEqual(reused["id"], revised["draft_id"])
        self.assertFalse(reused["published"])
        image = self.attachments.save("pixel.png", b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        media_db = self.media._open()
        media_db.execute(
            "INSERT INTO media_save_jobs(id,candidate_id,source_url,source_post_id,"
            "media_index,state,output_name,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("job-video", "source:123", "https://media.tumblr.com/video.mp4", "123",
             0, "completed", "clip.mp4", 1, 1))
        media_db.commit()
        media_db.close()
        bound = perform("publisher.04", {
            "draft_id": reused["id"], "attachment_ids": [image["id"]],
            "media_job_ids": ["job-video"]}, drafts=self.drafts,
            attachments=self.attachments, media=self.media)
        self.assertFalse(bound["tumblr_upload"])
        self.assertEqual(bound["media_jobs"][0]["id"], "job-video")
        suggested = perform("publisher.05", {"draft_id": reused["id"]},
                            drafts=self.drafts, discovery=self.discovery)
        self.assertEqual(suggested["suggestions"][:2], ["art", "night"])
        applied = perform("publisher.05", {"draft_id": reused["id"], "apply": ["art"]},
                          drafts=self.drafts, discovery=self.discovery)
        self.assertEqual(applied["applied"], ["art"])
        self.assertFalse(applied["published"])

    def test_campaign_copy_and_winner_notice_use_existing_ledger(self):
        now = {"value": 1000}
        contest = CampaignContest(
            self.db, clock=lambda: now["value"], eligibility=lambda *_: True)
        contest.create("launch", "賞品", deadline=1100, winners=1,
                       claim_seconds=60, gate_topic_id="100")
        contest.enter("launch", "alice")
        now["value"] = 1101
        contest.draw("launch")
        copy = perform("campaign.02", {"campaign_id": "launch"},
                       drafts=self.drafts, campaign_db=self.db)
        self.assertIn("賞品", copy["text"])
        self.assertFalse(copy["notified"])
        event_id = contest.pending_notifications("launch")[0]["event_id"]

        class Owner:
            def perform(self, action, target, payload):
                self.seen = (action, target, payload["operation_key"])
                return {"verified": True, "reference": "message-1"}

        owner = Owner()
        sent = perform("campaign.08", {"campaign_id": "launch", "event_id": event_id},
                       drafts=self.drafts, campaign_db=self.db, telegram=owner)
        self.assertEqual(sent["state"], "verified")
        self.assertEqual(owner.seen[0], "telegram.publish_announcement")
        self.assertEqual(owner.seen[1], "100")

    def test_referral_share_configuration_and_confirmed_send(self):
        configured = perform("referral.04", {"campaign_id": "ref", "name": "紹介"},
                             drafts=self.drafts, campaign_db=self.db)
        self.assertEqual(configured["state"], "configured")
        again = perform("referral.04", {"campaign_id": "ref", "name": "別"},
                        drafts=self.drafts, campaign_db=self.db)
        self.assertEqual(again["state"], "already_configured")
        self.assertEqual(again["name"], "紹介")
        shared = perform("referral.02", {"campaign_id": "ref", "referrer_id": "owner"},
                         drafts=self.drafts, campaign_db=self.db)
        self.assertIsNone(shared["public_url"])
        self.assertIn(shared["referral_code"], shared["text"])
        prepared = perform("referral.03", {"campaign_id": "ref", "source_post_id": "post-1"},
                           drafts=self.drafts, campaign_db=self.db)
        self.assertFalse(prepared["shared"])
        with self.assertRaisesRegex(ValueError, "referral_share_not_connected"):
            perform("referral.03", {
                "campaign_id": "ref", "source_post_id": "post-1",
                "destination": "topic", "confirmed": True},
                drafts=self.drafts, campaign_db=self.db, telegram=None)

        class Owner:
            def perform(self, action, target, payload):
                return {"verified": True, "reference": "message-2"}

        sent = perform("referral.03", {
            "campaign_id": "ref", "source_post_id": "post-1",
            "destination": "topic", "confirmed": True},
            drafts=self.drafts, campaign_db=self.db, telegram=Owner())
        self.assertTrue(sent["shared"])
        self.assertIsNone(sent["public_url"])

    def test_inflow_separates_telegram_linked_and_unclassified_events(self):
        record_event(self.db, "e1", "entry", "telegram:group")
        record_event(self.db, "e2", "entry", "mystery-source")
        record_event(self.db, "e3", "click", "tumblr")
        telegram = perform("analytics.09", {}, drafts=self.drafts, analytics=self.analytics)
        unknown = perform("analytics.10", {}, drafts=self.drafts, analytics=self.analytics)
        self.assertEqual(telegram["total"], 1)
        self.assertEqual(telegram["items"][0]["source"], "telegram:group")
        self.assertEqual(unknown["total"], 1)
        self.assertEqual(unknown["items"][0]["source"], "mystery-source")
        self.assertIn("転換率", telegram["not_measured"])

    def test_rejects_unknown_operation_without_a_side_effect(self):
        with self.assertRaisesRegex(ValueError, "catalog_operation_invalid"):
            perform("publisher.99", {"text": "nope"}, drafts=self.drafts)
        self.assertEqual(self.drafts.count(), 0)


if __name__ == "__main__":
    unittest.main()
