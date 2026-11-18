"""Observed (not estimated) Analytics through the actual private candidate store."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.analytics_runtime import AnalyticsAdapter
from tumblrkit.core import connect, create_campaign, issue_referral, confirm_membership
from tumblrkit.integration import CoreLinkStore
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.library import Library
from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge


class AnalyticsRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder=Path(temp.name)
        os.chmod(self.folder,0o700)
        self.now=2_000_000_000
        self.store=DiscoveryStore(self.folder/"discovery.sqlite3",
                                  clock=lambda: self.now-10*86400)
        self.add("older",1,"image")
        self.store.clock=lambda: self.now
        self.add("newer",2,"video")
        self.add("newer",3,"image")
        self.library=Library(self.store)
        self.library.update(["newer:2"],"seen",True)
        self.library.update(["newer:2"],"favorite",True)
        self.library.update(["newer:3"],"watch_later",True)
        self.adapter=AnalyticsAdapter(self.store,clock=lambda: self.now)

    def add(self,blog,number,kind):
        self.store.save_page("tag:metric",[
            {"name":blog,"url":"https://"+blog+".tumblr.com"}],[
            {"candidate_id":f"{blog}:{number}","blog":blog,"post_id":str(number),
             "url":f"https://{blog}.tumblr.com/post/{number}",
             "title":"real stored candidate","type":kind,"date":"",
             "thumbnail_url":"","text_excerpt":"","tags":[],"note_count":0,
             "media_refs":[]}
        ],number,complete=True)

    def test_period_comparison_uses_disjoint_windows_and_measured_receipts(self):
        result=self.adapter.compare("week")
        self.assertTrue(result["verified"])
        self.assertEqual(result["current"]["new_candidates"],2)
        self.assertEqual(result["previous"]["new_candidates"],1)
        self.assertEqual(result["current"]["verified_engagements"],0)
        self.assertEqual(result["previous"]["verified_engagements"],0)
        self.assertEqual(result["current"]["from"],result["previous"]["to"])
        self.assertEqual(result["current"]["to"]-result["current"]["from"],
                         result["previous"]["to"]-result["previous"]["from"])
        from tumblrkit.engagement_runtime import RECEIPTS
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            db.executescript(RECEIPTS)
            db.executemany(
                "INSERT INTO engagement_receipts(action,target,state,updated_at) "
                "VALUES(?,?,?,?)",[
                    ("engagement.like","newer:2","verified",self.now-1),
                    ("engagement.reblog","older:1","verified",self.now-8*86400),
                    ("engagement.follow","newer","reserved",self.now),
                ])
        comparison=self.adapter.compare("week")
        self.assertEqual(comparison["current"]["verified_engagements"],1)
        self.assertEqual(comparison["previous"]["verified_engagements"],1)
        self.store.exclude_blog("newer")
        hidden=self.adapter.compare("week")
        self.assertEqual(hidden["current"]["new_candidates"],0)
        self.assertEqual(hidden["current"]["verified_engagements"],1)
        for invalid in ("all","year","",None,3):
            with self.assertRaisesRegex(ValueError,"analytics_comparison_period_invalid"):
                self.adapter.compare(invalid)

    def test_csv_report_contains_only_verified_local_summary_and_unmeasured_note(self):
        import csv
        import io
        content=self.adapter.report_csv("week")
        self.assertTrue(content.startswith(chr(0xfeff)))
        self.assertIn("\\r\\n",repr(content))
        rows=list(csv.DictReader(io.StringIO(content.lstrip(chr(0xfeff)))))
        self.assertEqual(rows[0]["metric"],"確認済み候補数")
        self.assertEqual(rows[0]["value"],"2")
        self.assertEqual(rows[-1]["metric"],"UNMEASURED")
        self.assertIn("Telegram参加転換率",rows[-1]["basis"])
        for bad in ("yesterday","",None,7):
            with self.assertRaisesRegex(ValueError,"analytics_period_invalid"):
                self.adapter.report_csv(bad)

    def test_post_reactions_separates_observed_notes_from_own_verified_actions(self):
        from tumblrkit.engagement_runtime import RECEIPTS
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE discovery_posts SET note_count=7 WHERE candidate_id='newer:2'")
            db.executescript(RECEIPTS)
            db.executemany(
                "INSERT INTO engagement_receipts(action,target,state,updated_at) "
                "VALUES(?,?,?,?)",[
                    ("engagement.like","newer:2","verified",self.now),
                    ("engagement.reblog","newer:2","sent",self.now),
                ])
        result=self.adapter.post_reactions("newer:2")
        self.assertTrue(result["verified"])
        self.assertEqual(result["observed_notes"],7)
        self.assertEqual(result["observed_at"],self.now)
        self.assertEqual([a["action"] for a in result["confirmed_own_actions"]],
                         ["engagement.like"])
        self.assertIn("スキのみの総数",result["not_measured"])
        self.store.mark_unavailable("newer:2")
        with self.assertRaisesRegex(ValueError,"analytics_candidate_unavailable"):
            self.adapter.post_reactions("newer:2")
        for candidate in (None,"bad",{},1):
            with self.assertRaisesRegex(ValueError,"analytics_candidate_invalid"):
                self.adapter.post_reactions(candidate)

    def test_week_metrics_are_derived_from_current_real_state(self):
        result=self.adapter.perform("analytics.get_summary","week",{})
        self.assertTrue(result["verified"])
        self.assertEqual(result["basis"],"owner_private_discovery_library")
        self.assertEqual([r["value"] for r in result["rows"]],[2,1,1,1,1,0,0,0])
        self.assertEqual(result["rows"][0]["period"],"過去7日間")
        self.assertIn("Tumblrフォロワー数",result["not_measured"])

    def test_all_time_counts_and_exclusions_are_respected(self):
        total=self.adapter.perform("analytics.get_summary","all",{})
        self.assertEqual([r["value"] for r in total["rows"]],[3,1,1,1,1,0,0,0])
        self.store.exclude_blog("newer")
        after=self.adapter.perform("analytics.get_summary","all",{})
        self.assertEqual([r["value"] for r in after["rows"]],[1,0,0,0,0,0,0,0])

    def test_only_provider_readback_verified_engagement_counts_as_real(self):
        # These are deliberately seeded owner-private receipts, not Tumblr
        # follower counts or guessed public reactions.
        from tumblrkit.engagement_runtime import RECEIPTS
        import sqlite3
        with sqlite3.connect(self.store.path) as db:
            db.executescript(RECEIPTS)
            db.executemany(
                "INSERT INTO engagement_receipts(action,target,state,updated_at) "
                "VALUES(?,?,?,?)",[
                    ("engagement.like","newer:2","verified",self.now),
                    ("engagement.like","newer:3","reserved",self.now),
                    ("engagement.reblog","older:1","verified",self.now-10*86400),
                    ("engagement.follow","newer","sent",self.now),
                    ("engagement.follow","older","verified",self.now),
                ])
        week=self.adapter.perform("analytics.get_summary","week",{})
        self.assertEqual([x["value"] for x in week["rows"]][-3:],[1,0,1])
        self.assertEqual(week["engagement_basis"],
                         "official_oauth_readback_receipts")
        all_time=self.adapter.perform("analytics.get_summary","all",{})
        self.assertEqual([x["value"] for x in all_time["rows"]][-3:],[1,1,1])
        self.assertIn("Tumblr反応総数",all_time["not_measured"])
        self.store.exclude_blog("newer")
        historical=self.adapter.perform("analytics.get_summary","all",{})
        # Excluding a later target hides Discovery recommendations, but must
        # not erase genuinely confirmed historical owner interactions.
        self.assertEqual([x["value"] for x in historical["rows"]][-3:],[1,1,1])

    def test_missing_engagement_table_means_zero_not_provider_fabrication(self):
        response=self.adapter.perform("analytics.get_summary","today",{})
        self.assertEqual([r["value"] for r in response["rows"]][-3:],[0,0,0])
        self.assertIn("Tumblrフォロワー数",response["not_measured"])

    def test_campaign_records_are_reported_only_from_injected_private_db(self):
        db=connect(str(self.folder/"campaign.sqlite3"))
        self.addCleanup(db.close)
        create_campaign(db,"campaign","verified records",{2:"tier"})
        code=issue_referral(db,"campaign","alice")
        links=CoreLinkStore(db)
        link=links.create("campaign","123")
        self.assertTrue(links.mark("click-1","entry",link))
        self.assertFalse(links.mark("click-1","entry",link))
        self.assertTrue(links.mark("click-2","entry",link))
        self.assertEqual(confirm_membership(db,"campaign","bob",
                         gate_verified=True,referral_code=code),[])
        self.assertEqual(confirm_membership(db,"campaign","charlie",
                         gate_verified=True,referral_code=code),["tier"])
        with db:
            db.execute("INSERT INTO announcement_receipts "
                       "(operation_key,payload_digest,destination,state,message_id,updated_at) "
                       "VALUES('sent','hash','group','sent','11','2033-05-01T00:00:00+00:00')")
            db.execute("INSERT INTO announcement_receipts "
                       "(operation_key,payload_digest,destination,state,message_id,updated_at) "
                       "VALUES('verified','hash2','group','verified','12','2033-05-01T00:00:00+00:00')")
        report=AnalyticsAdapter(self.store,clock=lambda:self.now,
                                campaign_db=db).perform("analytics.get_summary","all",{})
        self.assertEqual(report["campaign_basis"],
                         "local_campaign_records_not_external_conversion")
        self.assertEqual([r["value"] for r in report["rows"]][-4:],[2,2,1,1])
        import csv,io
        csv_rows=list(csv.DictReader(io.StringIO(
            AnalyticsAdapter(self.store,clock=lambda:self.now,campaign_db=db)
            .report_csv("all").lstrip(chr(0xfeff)))))
        campaign_rows=[r for r in csv_rows if r["metric"].startswith("アプリ記録済み")]
        self.assertTrue(campaign_rows)
        self.assertTrue(all(r["basis"]=="local_campaign_records_not_external_conversion"
                            for r in campaign_rows))
        self.assertIn("Telegram参加転換率",report["not_measured"])
        absent=self.adapter.perform("analytics.get_summary","all",{})
        self.assertEqual(absent["campaign_basis"],"not_connected")
        self.assertEqual(len(absent["rows"]),8)

    def test_persistent_campaign_source_reopens_after_previous_connection_closes(self):
        path = self.folder / "drafts.sqlite3"
        db = connect(str(path))
        create_campaign(db, "persist", "Existing private owner ledger", {})
        link = CoreLinkStore(db).create("persist", "post")
        CoreLinkStore(db).mark("entry", "entry", link)
        db.close()
        report = AnalyticsAdapter(self.store, clock=lambda:self.now,
                                  campaign_db_path=path).perform(
                                      "analytics.get_summary", "all", {})
        self.assertEqual(report["campaign_basis"],
                         "local_campaign_records_not_external_conversion")
        self.assertEqual([x["value"] for x in report["rows"]][-4:], [1,0,0,0])
        self.assertTrue(path.is_file())

    def test_nonexistent_campaign_source_is_not_created_by_reporting(self):
        missing = self.folder / "missing.sqlite3"
        adapter = AnalyticsAdapter(self.store, clock=lambda:self.now,
                                   campaign_db_path=missing)
        with self.assertRaisesRegex(ValueError, "analytics_campaign_source_unavailable"):
            adapter.perform("analytics.get_summary", "all", {})
        self.assertFalse(missing.exists())

    def test_campaign_entry_winner_and_claim_counts_are_local_not_notifications(self):
        from tumblrkit.campaign_runtime import CampaignContest
        db=connect(str(self.folder/"contest-ledger.sqlite3"))
        self.addCleanup(db.close)
        owner=CampaignContest(db,clock=lambda:self.now,eligibility=lambda *_:True)
        owner.create("lottery","Prize",deadline=self.now+60,winners=1,claim_seconds=120)
        owner.enter("lottery","alice")
        self.now+=61
        result=owner.draw("lottery")
        owner.claim("lottery",result.winners[0])
        report=AnalyticsAdapter(self.store,clock=lambda:self.now,campaign_db=db).perform(
            "analytics.get_summary","all",{})
        self.assertEqual([r["value"] for r in report["rows"]][-3:],[1,1,1])
        self.assertIn("通知未確認",report["rows"][-2]["label"])

    def test_verified_follower_snapshot_appears_in_summary_only_for_owner(self):
        from tumblrkit.follower_analytics import FollowerAnalytics
        class LocalTransport:
            def get(self,path,token):
                self_request=(path,token)
                assert self_request==("/v2/user/info","offline-token")
                return {"meta":{"status":200},"response":{"user":{"blogs":[
                    {"name":"owner","followers":42}]}}}
        class Owner:
            blog="owner"
            transport=LocalTransport()
            token_provider=lambda self:"offline-token"
        follower=FollowerAnalytics(Owner(),self.store,clock=lambda:self.now)
        self.assertTrue(follower.snapshot()["verified"])
        report=AnalyticsAdapter(self.store,clock=lambda:self.now,owner_blog="owner").perform(
            "analytics.get_summary","week",{})
        self.assertEqual(report["rows"][-1]["value"],42)
        self.assertNotIn("Tumblrフォロワー数",report["not_measured"])
        unrelated=AnalyticsAdapter(self.store,clock=lambda:self.now,owner_blog="other").perform(
            "analytics.get_summary","week",{})
        self.assertEqual(len(unrelated["rows"]),8)
        self.assertIn("Tumblrフォロワー数",unrelated["not_measured"])

    def test_partial_campaign_schema_does_not_fabricate_zeroes(self):
        db=connect(str(self.folder/"campaign-missing.sqlite3"))
        self.addCleanup(db.close)
        with db:
            db.execute("DROP TABLE announcement_receipts")
        adapter=AnalyticsAdapter(self.store,clock=lambda:self.now,campaign_db=db)
        with self.assertRaisesRegex(ValueError,"analytics_campaign_source_unavailable"):
            adapter.perform("analytics.get_summary","all",{})

    def test_invalid_period_or_payload_refuses_without_estimates(self):
        for value in ("week;DELETE","follower_growth","future",123):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.adapter.perform("analytics.get_summary",value,{})
        with self.assertRaises(ValueError):
            self.adapter.perform("analytics.get_summary","week",{"fake":999})

    def test_real_assistant_route_returns_verified_analytics_rows(self):
        assistant=Assistant(bridge=Bridge({"analytics":self.adapter}),providers=[])
        request=assistant.turn(selected_action="analytics.get_summary",
                               selected_target="week")
        self.assertEqual(request["state"],"completed")
        self.assertTrue(request["executed"])
        self.assertEqual(request["view"]["rows"][0]["label"],"確認済み候補数")
        self.assertEqual(request["view"]["rows"][0]["value"],"2")
        self.assertEqual(request["view"]["rows"][0]["period"],"過去7日間")
        self.assertEqual(request["plan"] if "plan" in request else None,None)


if __name__=="__main__":
    unittest.main()
