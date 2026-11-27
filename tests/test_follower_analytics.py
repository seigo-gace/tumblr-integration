"""No real Tumblr request: only official response-shaped local stubs."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.tumblr_read import TumblrReadAdapter
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.follower_analytics import FollowerAnalytics


class LocalOwnerTransport:
    def __init__(self):
        self.calls=[]
        self.followers=12
        self.blog="owner"
    def get(self,path,token):
        self.calls.append((path,token))
        return {"meta":{"status":200},"response":{"user":{"blogs":[
            {"name":self.blog,"followers":self.followers},
            {"name":"another","followers":100},
        ]}}}


class FollowerAnalyticsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)
        os.chmod(self.path,0o700)
        self.transport=LocalOwnerTransport()
        self.reader=TumblrReadAdapter(
            "owner",self.path/"none",
            transport=self.transport,token_provider=lambda:"fake-oauth-token")
        self.store=DiscoveryStore(self.path/"discovery.sqlite3")
        self.now=1700000000
        self.adapter=FollowerAnalytics(self.reader,self.store,clock=lambda:self.now)

    def test_owner_verified_count_persists_daily_and_history_never_fetches(self):
        first=self.adapter.snapshot()
        self.assertTrue(first["verified"])
        self.assertEqual(first["followers"],12)
        self.assertEqual(self.transport.calls,[("/v2/user/info","fake-oauth-token")])
        self.transport.followers=13
        self.now+=60
        self.adapter.snapshot()
        self.now+=86400
        self.transport.followers=15
        self.adapter.snapshot()
        reloaded=FollowerAnalytics(self.reader,self.store,clock=lambda:self.now)
        report=reloaded.history()
        self.assertEqual([r["followers"] for r in report["rows"]],[13,15])
        self.assertEqual(report["provider_calls"],0)
        self.assertEqual(len(self.transport.calls),3)

    def test_wrong_owner_and_missing_or_invalid_count_not_saved(self):
        self.transport.blog="other"
        with self.assertRaisesRegex(ValueError,"analytics_owner_not_verified"):
            self.adapter.snapshot()
        self.transport.blog="owner"
        self.transport.followers="99"
        with self.assertRaisesRegex(ValueError,"analytics_followers_not_reported"):
            self.adapter.snapshot()
        self.assertEqual(self.adapter.history()["rows"],[])

    def test_missing_oauth_fails_closed_without_fetch_or_fake_zero(self):
        missing=FollowerAnalytics(None,self.store)
        with self.assertRaisesRegex(ValueError,"analytics_oauth_not_connected"):
            missing.snapshot()
        with self.assertRaisesRegex(ValueError,"analytics_oauth_not_connected"):
            missing.history()
        self.assertEqual(self.transport.calls,[])


if __name__=="__main__":
    unittest.main()
