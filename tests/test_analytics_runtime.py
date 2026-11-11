"""Observed (not estimated) Analytics through the actual private candidate store."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.analytics_runtime import AnalyticsAdapter
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
