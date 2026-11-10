"""Complete offline Discovery module acceptance: following/followers/tag, cursor,
watermark, de-duplication, private durable handoff, exclusions and failure recovery.
Zero external API calls in this test.
"""
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tumblrkit.discovery import DiscoveryEngine
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.tumblr_read import TumblrReadAdapter


class FakePages:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
    def get(self, path, token):
        self.calls.append(path)
        if not self.pages:
            raise AssertionError("Unexpected external API call")
        response=self.pages.pop(0)
        if isinstance(response,BaseException):
            raise response
        return response


def envelope(payload):
    return {"meta":{"status":200},"response":payload}


class DiscoveryEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        os.chmod(self.root,0o700)
        self.token=self.root/"access-token"
        self.token.write_text("not-real-test-oauth-token",encoding="utf-8")
        os.chmod(self.token,0o600)

    def scanner(self,pages):
        stub=FakePages(pages)
        return TumblrReadAdapter("owner",self.token,transport=stub),stub

    def test_following_pagination_persists_across_restart_and_dedupes(self):
        page1=envelope({"total_blogs":11,"blogs":[
            {"name":"first"},{"name":"second"},{"name":"second"}]})
        page2=envelope({"total_blogs":11,"blogs":[{"name":"third"}]})
        adapter,stub=self.scanner([page1,page2])
        result=adapter.perform("tumblr.discover","following",{})
        self.assertTrue(result["verified"])
        self.assertEqual(result["cursor"],3)
        self.assertFalse(result["complete"])
        self.assertEqual({row["title"] for row in result["rows"]},{"first","second"})
        self.assertIn("offset=0",stub.calls[0])
        # New adapter/connection resumes from the committed cursor.
        next_adapter=TumblrReadAdapter("owner",self.token,transport=stub)
        result=next_adapter.perform("tumblr.discover","following",{})
        self.assertEqual(result["cursor"],4)
        self.assertIn("offset=3",stub.calls[1])
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        self.assertEqual(len(store.read_blogs("following:following")),3)

    def test_followers_scope_is_owner_blog_only(self):
        adapter,stub=self.scanner([envelope({
            "total_users":1,"users":[{"name":"reader"}]})])
        result=adapter.perform("tumblr.discover","followers",{})
        self.assertEqual(len(result["rows"]),1)
        self.assertTrue(result["complete"])
        self.assertEqual(len(stub.calls),1)
        self.assertTrue(stub.calls[0].startswith("/v2/blog/owner.tumblr.com/followers?"))
        # Completed source uses cached candidates without a new request.
        cached=adapter.perform("tumblr.discover","followers",{})
        self.assertEqual(len(stub.calls),1)
        self.assertTrue(cached["from_cache"])

    def test_tags_resume_timestamp_and_exclude_blog(self):
        posts=[{"blog_name":"other","id_string":str(i),
                "post_url":"https://other.tumblr.com/post/"+str(i),
                "summary":"post","timestamp":100+i,"date":"2026-11-11"} for i in range(10)]
        adapter,stub=self.scanner([envelope(posts),envelope([])])
        first=adapter.perform("tumblr.discover","related:focus",{})
        self.assertFalse(first["complete"])
        self.assertEqual(first["cursor"],100)
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        store.exclude_blog("other")
        self.assertEqual(store.read_candidates(source="related:focus"),[])
        second=adapter.perform("tumblr.discover","related:focus",{})
        self.assertTrue(second["complete"])
        self.assertEqual(parse_qs(urlsplit(stub.calls[1]).query)["before"],["100"])
        self.assertEqual(len(stub.calls),2)

    def test_invalid_response_does_not_advance_checkpoint(self):
        adapter,stub=self.scanner([envelope({"total_users":1,"users":"bad"})])
        with self.assertRaisesRegex(ValueError,"unverified"):
            adapter.perform("tumblr.discover","followers",{})
        checkpoint=DiscoveryStore(self.root/"discovery.sqlite3").checkpoint("followers:followers")
        self.assertEqual(checkpoint["cursor"],0)
        self.assertFalse(checkpoint["complete"])

    def test_failed_page_preserves_prior_committed_cursor(self):
        adapter,stub=self.scanner([
            envelope({"total_blogs":100,"blogs":[{"name":"one"}]}),
            ValueError("simulated provider outage"),
        ])
        adapter.perform("tumblr.discover","following",{})
        with self.assertRaises(ValueError):
            adapter.perform("tumblr.discover","following",{})
        checkpoint=DiscoveryStore(self.root/"discovery.sqlite3").checkpoint("following:following")
        self.assertEqual(checkpoint["cursor"],1)
        self.assertFalse(checkpoint["complete"])

    def test_existing_discovery_database_migrates_without_losing_records(self):
        import sqlite3
        dbpath=self.root/"discovery.sqlite3"
        with sqlite3.connect(dbpath) as db:
            db.execute("CREATE TABLE discovery_posts(candidate_id TEXT PRIMARY KEY, blog TEXT, post_id TEXT,"
                       "source TEXT,url TEXT,title TEXT,post_type TEXT,posted_at TEXT,thumbnail_url TEXT,"
                       "text_excerpt TEXT,tags_json TEXT,note_count INTEGER,state TEXT,first_seen INTEGER,last_seen INTEGER)")
            db.execute("INSERT INTO discovery_posts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(
                "legacy:7","legacy","7","tag:old","https://legacy.tumblr.com/post/7","old","text",
                "2026-11-10","","body","[]",0,"visible",1,1))
        store=DiscoveryStore(dbpath)
        rows=store.read_candidates()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["candidate_id"],"legacy:7")
        self.assertEqual(rows[0]["media_json"],"[]")

    def test_preserves_all_safe_image_video_media_refs_for_later_library(self):
        post={"blog_name":"one","id_string":"501","post_url":"https://one.tumblr.com/post/501",
              "summary":"media","timestamp":300,"content":[
                  {"type":"image","media":[{"url":"https://64.media.tumblr.com/a.jpg"},
                                           {"url":"https://64.media.tumblr.com/b.jpg"}]},
                  {"type":"video","poster":[{"url":"https://64.media.tumblr.com/c.jpg"}]},
                  {"type":"image","media":[{"url":"https://evil.invalid/out.jpg"}]}]}
        adapter,_=self.scanner([envelope([post])])
        adapter.perform("tumblr.discover","tag:media",{})
        record=DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()[0]
        import json
        refs=json.loads(record["media_json"])
        self.assertEqual([item["url"] for item in refs],[
            "https://64.media.tumblr.com/a.jpg",
            "https://64.media.tumblr.com/b.jpg",
            "https://64.media.tumblr.com/c.jpg",
        ])
        self.assertEqual(record["thumbnail_url"],"https://64.media.tumblr.com/a.jpg")

    def test_one_blog_can_be_present_in_multiple_sources_without_losing_links(self):
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        blog={"name":"one","url":"https://one.tumblr.com"}
        store.save_page("following:following",[blog],[],1)
        store.save_page("followers:followers",[blog],[],1)
        self.assertEqual(len(store.read_blogs("following:following")),1)
        self.assertEqual(len(store.read_blogs("followers:followers")),1)
        store.exclude_blog("one")
        self.assertEqual(store.read_blogs("following:following"),[])
        self.assertEqual(store.read_blogs("followers:followers"),[])

    def test_refresh_is_explicit_and_keeps_checkpoint_on_failure(self):
        adapter,stub=self.scanner([
            envelope({"total_users":1,"users":[{"name":"one"}]}),
            ValueError("transient official API outage"),
            envelope({"total_users":1,"users":[{"name":"two"}]}),
        ])
        adapter.perform("tumblr.discover","followers",{})
        self.assertEqual(len(stub.calls),1)
        cached=adapter.perform("tumblr.discover","followers",{})
        self.assertTrue(cached["from_cache"])
        self.assertEqual(len(stub.calls),1)
        with self.assertRaises(ValueError):
            adapter.perform("tumblr.discover","refresh:followers",{})
        self.assertEqual(DiscoveryStore(self.root/"discovery.sqlite3").checkpoint("followers:followers")["cursor"],1)
        updated=adapter.perform("tumblr.discover","refresh:followers",{})
        self.assertTrue(updated["complete"])
        self.assertEqual(len(stub.calls),3)
        self.assertIn("offset=0",stub.calls[-1])

    def test_opt_in_candidate_availability_exact_id_and_unavailable(self):
        initial=envelope([{"blog_name":"one","id_string":"991",
            "post_url":"https://one.tumblr.com/post/991","summary":"post","timestamp":211}])
        adapter,stub=self.scanner([initial,ValueError("tumblr_post_unavailable")])
        adapter.perform("tumblr.discover","tag:valid",{})
        from tumblrkit.discovery import DiscoveryEngine
        engine=DiscoveryEngine("owner",self.token,stub)
        result=engine.recheck_candidate("one:991")
        self.assertEqual(result["state"],"unavailable")
        self.assertIn("id=991",stub.calls[-1])
        self.assertEqual(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates(),[])

    def test_network_failure_never_marks_candidate_deleted(self):
        initial=envelope([{"blog_name":"one","id_string":"992",
            "post_url":"https://one.tumblr.com/post/992","summary":"post","timestamp":212}])
        adapter,stub=self.scanner([initial,ValueError("network-failure")])
        adapter.perform("tumblr.discover","tag:valid",{})
        from tumblrkit.discovery import DiscoveryEngine
        with self.assertRaises(ValueError):
            DiscoveryEngine("owner",self.token,stub).recheck_candidate("one:992")
        self.assertEqual(len(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()),1)

    def test_local_unavailable_state_not_silent_deletion(self):
        adapter,_=self.scanner([envelope([{
            "blog_name":"one","id_string":"999","post_url":"https://one.tumblr.com/post/999",
            "summary":"post","timestamp":200
        }])])
        adapter.perform("tumblr.discover","tag:focus",{})
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        self.assertEqual(len(store.read_candidates()),1)
        store.mark_unavailable("one:999")
        self.assertEqual(store.read_candidates(),[])


if __name__=="__main__":
    unittest.main()
