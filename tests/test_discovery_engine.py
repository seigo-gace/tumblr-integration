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

    def test_more_than_hundred_saved_posts_are_all_reachable_without_provider(self):
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        posts=[{
            "candidate_id":f"first:{i}","blog":"first","post_id":str(i),
            "url":f"https://first.tumblr.com/post/{i}","title":f"Post {i}",
            "type":"text","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,"media_refs":[],
        } for i in range(135)]
        store.save_page("tag:many",
            [{"name":"first","url":"https://first.tumblr.com"}],
            posts,135,complete=True)
        adapter,stub=self.scanner([])
        first=adapter.perform("tumblr.discover","saved:0:tag:many",{})
        self.assertEqual(first["total"],135)
        self.assertEqual(len(first["rows"]),100)
        self.assertEqual(first["stored_next_target"],"saved:100:tag:many")
        self.assertIsNone(first["stored_prev_target"])
        second=adapter.perform("tumblr.discover",first["stored_next_target"],{})
        self.assertEqual(second["total"],135)
        self.assertEqual(len(second["rows"]),35)
        self.assertEqual(second["stored_prev_target"],"saved:0:tag:many")
        self.assertIsNone(second["stored_next_target"])
        visited={r["candidate_id"] for r in first["rows"]+second["rows"]}
        self.assertEqual(len(visited),135)
        self.assertEqual(stub.calls,[])
        self.assertEqual(store.checkpoint("tag:many")["cursor"],135)
        store.exclude_blog("first")
        filtered=adapter.perform("tumblr.discover","saved:0:tag:many",{})
        self.assertEqual(filtered["total"],0)
        self.assertEqual(filtered["rows"],[])

    def test_over_hundred_following_blogs_use_local_pages_and_actual_totals(self):
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        blogs=[{"name":f"blog{i}","url":f"https://blog{i}.tumblr.com"} for i in range(125)]
        store.save_page("following:following",blogs,[],125,complete=True)
        adapter,stub=self.scanner([])
        first=adapter.perform("tumblr.discover","following",{})
        self.assertEqual(first["total"],125)
        self.assertEqual(len(first["rows"]),100)
        second=adapter.perform("tumblr.discover","saved:100:following:following",{})
        self.assertEqual(second["total"],125)
        self.assertEqual(len(second["rows"]),25)
        self.assertEqual(len({r["blog"] for r in first["rows"]+second["rows"]}),125)
        self.assertEqual(stub.calls,[])

    def test_saved_page_guards_reject_invalid_offset_without_provider_calls(self):
        adapter,stub=self.scanner([])
        for target in ("saved:-100:tag:focus","saved:1:tag:focus",
                       "saved:0100:tag:focus","saved:100:notreal:target",
                       "saved:999999999999:tag:focus"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    adapter.perform("tumblr.discover",target,{})
        self.assertEqual(stub.calls,[])

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

    def test_followed_blog_posts_become_library_candidates_with_source_and_resume(self):
        post=lambda i: {"blog_name":"first","id_string":str(i),
            "post_url":"https://first.tumblr.com/post/"+str(i),
            "summary":"real candidate","timestamp":2000-i,
            "content":[{"type":"image","media":[{"url":"https://64.media.tumblr.com/"+str(i)+".jpg"}]}]}
        first_page=[post(i) for i in range(10)]
        adapter,stub=self.scanner([
            envelope({"total_blogs":1,"blogs":[{"name":"first"}]}),
            envelope({"blog":{"name":"first"},"posts":first_page}),
            envelope({"blog":{"name":"first"},"posts":[post(20)]}),
        ])
        adapter.perform("tumblr.discover","following",{})
        result=adapter.perform("tumblr.discover","blog:first",{})
        self.assertEqual(len(result["rows"]),10)
        self.assertFalse(result["complete"])
        self.assertEqual(result["cursor"],10)
        self.assertIn("/v2/blog/first.tumblr.com/posts?",stub.calls[1])
        self.assertIn("npf=true",stub.calls[1])
        self.assertIn("offset=0",stub.calls[1])
        from tumblrkit.library import Library
        listing=Library(DiscoveryStore(self.root/"discovery.sqlite3")).list()
        self.assertEqual(listing["total"],10)
        self.assertEqual(listing["items"][0]["blog"],"first")
        resumed=TumblrReadAdapter("owner",self.token,transport=stub)
        result=resumed.perform("tumblr.discover","blog:first",{})
        self.assertTrue(result["complete"])
        self.assertIn("offset=10",stub.calls[2])
        self.assertEqual(Library(DiscoveryStore(self.root/"discovery.sqlite3")).list()["total"],11)

    def test_blog_offset_recovers_posts_with_identical_second_timestamps(self):
        posts=lambda start: [{
            "blog_name":"first","id_string":str(i),
            "post_url":"https://first.tumblr.com/post/"+str(i),
            "timestamp":1234567890,"summary":"same second"
        } for i in range(start,start+10)]
        adapter,stub=self.scanner([
            envelope({"total_blogs":1,"blogs":[{"name":"first"}]}),
            envelope({"blog":{"name":"first"},"posts":posts(100)}),
            envelope({"blog":{"name":"first"},"posts":posts(110)}),
            envelope({"blog":{"name":"first"},"posts":[]}),
        ])
        adapter.perform("tumblr.discover","following",{})
        first=adapter.perform("tumblr.discover","blog:first",{})
        self.assertEqual(first["cursor"],10)
        self.assertFalse(first["complete"])
        second=adapter.perform("tumblr.discover","blog:first",{})
        self.assertEqual(second["cursor"],20)
        self.assertEqual(len(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()),20)
        self.assertIn("offset=10",stub.calls[2])
        third=adapter.perform("tumblr.discover","blog:first",{})
        self.assertTrue(third["complete"])
        self.assertIn("offset=20",stub.calls[3])
        self.assertEqual(len(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()),20)

    def test_blog_offset_can_page_without_timestamps(self):
        records=[{"blog_name":"first","id_string":str(i),
                  "post_url":"https://first.tumblr.com/post/"+str(i)}
                 for i in range(20,30)]
        adapter,stub=self.scanner([
            envelope({"total_blogs":1,"blogs":[{"name":"first"}]}),
            envelope({"blog":{"name":"first"},"posts":records}),
            envelope({"blog":{"name":"first"},"posts":[]}),
        ])
        adapter.perform("tumblr.discover","following",{})
        page=adapter.perform("tumblr.discover","blog:first",{})
        self.assertEqual(page["cursor"],10)
        self.assertFalse(page["complete"])
        self.assertEqual(len(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()),10)
        page=adapter.perform("tumblr.discover","blog:first",{})
        self.assertTrue(page["complete"])
        self.assertIn("offset=10",stub.calls[2])

    def test_legacy_timestamp_checkpoint_is_preserved_not_reinterpreted(self):
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        store.save_page("following:following",[{"name":"first","url":"https://first.tumblr.com"}],[],1,True)
        store.save_page("blog:first",[],[],1762891200,False)
        adapter,stub=self.scanner([
            envelope({"blog":{"name":"first"},"posts":[]}),
        ])
        result=adapter.perform("tumblr.discover","blog:first",{})
        self.assertTrue(result["complete"])
        self.assertIn("before=1762891200",stub.calls[0])
        self.assertNotIn("offset=1762891200",stub.calls[0])
        # Explicit owner refresh resets the legacy cursor safely.
        # A failed refresh keeps persisted checkpoint unchanged.
        with self.assertRaises(AssertionError):
            adapter.perform("tumblr.discover","refresh:blog:first",{})
        self.assertEqual(store.checkpoint("blog:first")["cursor"],1762891200)

    def test_existing_assistant_action_reaches_selected_blog_posts_and_library(self):
        from tumblrkit.assistant import Assistant
        from tumblrkit.bridge import Bridge
        adapter,stub=self.scanner([
            envelope({"total_blogs":1,"blogs":[{"name":"first"}]}),
            envelope({"blog":{"name":"first"},"posts":[{
                "blog_name":"first","id_string":"500",
                "post_url":"https://first.tumblr.com/post/500",
                "summary":"candidate","timestamp":1000,
                "content":[{"type":"video","media":[{"url":"https://64.media.tumblr.com/v.mp4"}]}],
            }]}),
        ])
        assistant=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[])
        first=assistant.turn("検索して 対象: following")
        self.assertEqual(first["state"],"completed")
        result=assistant.turn("検索して 対象: blog:first")
        self.assertEqual(result["state"],"completed")
        self.assertEqual(result["view"]["rows"][0]["title"],"candidate")
        from tumblrkit.library import Library
        records=Library(DiscoveryStore(self.root/"discovery.sqlite3")).list()
        self.assertEqual(records["total"],1)
        self.assertEqual(records["items"][0]["media"][0]["type"],"video")
        self.assertEqual(len(stub.calls),2)

    def test_unselected_or_excluded_blog_does_not_fetch_any_posts(self):
        adapter,stub=self.scanner([envelope({"total_blogs":1,"blogs":[{"name":"first"}]})])
        adapter.perform("tumblr.discover","following",{})
        with self.assertRaisesRegex(ValueError,"discovery_blog_not_selected"):
            adapter.perform("tumblr.discover","blog:other",{})
        self.assertEqual(len(stub.calls),1)
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        store.exclude_blog("first")
        with self.assertRaisesRegex(ValueError,"discovery_blog_not_selected"):
            adapter.perform("tumblr.discover","blog:first",{})
        self.assertEqual(len(stub.calls),1)

    def test_blog_identity_mismatch_never_commits_candidates_or_cursor(self):
        adapter,stub=self.scanner([
            envelope({"total_users":1,"users":[{"name":"first"}]}),
            envelope({"blog":{"name":"other"},"posts":[]}),
        ])
        adapter.perform("tumblr.discover","followers",{})
        with self.assertRaisesRegex(ValueError,"discovery_blog_identity_unverified"):
            adapter.perform("tumblr.discover","blog:first",{})
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        self.assertEqual(store.checkpoint("blog:first")["cursor"],0)
        self.assertEqual(store.read_candidates(),[])

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

    def test_related_tags_are_observed_not_invented_and_are_actionable(self):
        from tumblrkit.assistant import Assistant
        from tumblrkit.bridge import Bridge
        posts=[
            {"blog_name":"first","id_string":"71",
             "post_url":"https://first.tumblr.com/post/71",
             "summary":"one","timestamp":300,"tags":["focus","art","video","art"]},
            {"blog_name":"second","id_string":"72",
             "post_url":"https://second.tumblr.com/post/72",
             "summary":"two","timestamp":299,"tags":["focus","video","tools","invalid/?"]},
        ]
        adapter,stub=self.scanner([envelope(posts)])
        output=Assistant(bridge=Bridge({"tumblr":adapter}),providers=[]).turn(
            "検索して 対象: related:focus")
        self.assertEqual(output["state"],"completed")
        view=output["view"]
        self.assertEqual(view["related_basis"],"observed_cooccurring_tags")
        self.assertEqual(view["related_tags"],[
            {"tag":"video","count":2,"target":"tag:video"},
            {"tag":"art","count":1,"target":"tag:art"},
            {"tag":"tools","count":1,"target":"tag:tools"},
        ])
        self.assertEqual(len(stub.calls),1)
        self.assertIn("tag=focus",stub.calls[0])
        # Cached read returns the same observed links without new provider use.
        repeat=adapter.perform("tumblr.discover","related:focus",{})
        self.assertTrue(repeat["from_cache"])
        self.assertEqual(repeat["related_tags"][0]["count"],2)
        self.assertEqual(len(stub.calls),1)

    def test_related_tags_count_all_saved_pages_not_only_displayed_hundred(self):
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        posts=[{
            "candidate_id":f"one:{i}","blog":"one","post_id":str(i),
            "url":f"https://one.tumblr.com/post/{i}","title":"candidate",
            "type":"text","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":["focus","late"] if i<25 else ["focus","regular"],
            "note_count":0,"media_refs":[],
        } for i in range(125)]
        store.save_page("related:focus",
                        [{"name":"one","url":"https://one.tumblr.com"}],
                        posts,1000,complete=True)
        adapter,stub=self.scanner([])
        first=adapter.perform("tumblr.discover","saved:0:related:focus",{})
        last=adapter.perform("tumblr.discover","saved:100:related:focus",{})
        self.assertEqual(first["stored_total"],125)
        self.assertEqual(len(first["rows"]),100)
        self.assertEqual(len(last["rows"]),25)
        expected=[
            {"tag":"regular","count":100,"target":"tag:regular"},
            {"tag":"late","count":25,"target":"tag:late"},
        ]
        self.assertEqual(first["related_tags"],expected)
        self.assertEqual(last["related_tags"],expected)
        self.assertEqual(stub.calls,[])
        store.exclude_blog("one")
        excluded=adapter.perform("tumblr.discover","saved:0:related:focus",{})
        self.assertEqual(excluded["related_tags"],[])
        self.assertEqual(excluded["total"],0)

    def test_related_search_without_verified_tags_does_not_fake_suggestions(self):
        adapter,stub=self.scanner([envelope([
            {"blog_name":"first","id_string":"90",
             "post_url":"https://first.tumblr.com/post/90",
             "summary":"none","timestamp":300,"tags":["focus"]},
        ])])
        output=adapter.perform("tumblr.discover","related:focus",{})
        self.assertEqual(output["related_tags"],[])
        self.assertEqual(output["related_basis"],"observed_cooccurring_tags")

    def test_featured_timestamp_used_for_tag_cursor(self):
        posts=[{"blog_name":"one","id_string":str(100+i),
                "post_url":"https://one.tumblr.com/post/"+str(100+i),
                "timestamp":999999,"featured_timestamp":300-i,
                "summary":"featured"} for i in range(10)]
        adapter,stub=self.scanner([envelope(posts),envelope([])])
        result=adapter.perform("tumblr.discover","tag:featured",{})
        self.assertEqual(result["cursor"],291)
        self.assertFalse(result["complete"])
        adapter.perform("tumblr.discover","tag:featured",{})
        self.assertEqual(parse_qs(urlsplit(stub.calls[1]).query)["before"],["291"])

    def test_tied_oldest_seconds_fail_closed_to_avoid_skipped_posts(self):
        posts=[{"blog_name":"one","id_string":str(200+i),
                "post_url":"https://one.tumblr.com/post/"+str(200+i),
                "timestamp":1000-i//2} for i in range(10)]
        adapter,stub=self.scanner([envelope(posts)])
        with self.assertRaisesRegex(ValueError,"discovery_pagination_boundary_ambiguous"):
            adapter.perform("tumblr.discover","tag:same-second",{})
        checkpoint=DiscoveryStore(self.root/"discovery.sqlite3").checkpoint("tag:same-second")
        self.assertEqual(checkpoint["cursor"],0)
        self.assertFalse(checkpoint["complete"])
        self.assertEqual(len(stub.calls),1)

    def test_realistic_unix_timestamp_cursor_persists(self):
        now=1762891200
        posts=[{"blog_name":"one","id_string":str(500+i),
                "post_url":"https://one.tumblr.com/post/"+str(500+i),
                "timestamp":now-i} for i in range(10)]
        adapter,stub=self.scanner([envelope(posts)])
        result=adapter.perform("tumblr.discover","tag:timestamp",{})
        self.assertEqual(result["cursor"],now-9)
        self.assertEqual(DiscoveryStore(self.root/"discovery.sqlite3")
                         .checkpoint("tag:timestamp")["cursor"],now-9)

    def test_oversized_provider_page_is_rejected_without_cursor_mutation(self):
        blogs=[{"name":"blog"+str(i)} for i in range(11)]
        adapter,stub=self.scanner([envelope({"total_blogs":30,"blogs":blogs})])
        with self.assertRaisesRegex(ValueError,"discovery_response_unverified"):
            adapter.perform("tumblr.discover","following",{})
        self.assertEqual(DiscoveryStore(self.root/"discovery.sqlite3")
                         .checkpoint("following:following")["cursor"],0)
        self.assertEqual(len(stub.calls),1)

    def test_missing_timestamp_full_page_stops_without_claiming_completion(self):
        posts=[{"blog_name":"one","id_string":str(300+i),
                "post_url":"https://one.tumblr.com/post/"+str(300+i)}
               for i in range(10)]
        adapter,stub=self.scanner([envelope(posts)])
        with self.assertRaisesRegex(ValueError,"discovery_page_timestamp_invalid"):
            adapter.perform("tumblr.discover","tag:missing-dates",{})
        self.assertEqual(DiscoveryStore(self.root/"discovery.sqlite3").read_candidates(),[])
        self.assertEqual(len(stub.calls),1)

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

    def test_official_custom_domain_permalink_still_preserves_real_candidate(self):
        from json import loads
        tags=["focus"]+[f"topic-{i}" for i in range(45)]
        item={"blog_name":"first","id_string":"880",
              "post_url":"https://external-custom-domain.example/posts/880",
              "timestamp":1770000000,"tags":tags,"summary":"published item"}
        adapter,stub=self.scanner([envelope([item])])
        result=adapter.perform("tumblr.discover","related:focus",{})
        self.assertEqual(result["total"],1)
        self.assertEqual(len(result["related_tags"]),8)
        stored=DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()[0]
        self.assertEqual(stored["url"],"https://first.tumblr.com/post/880")
        self.assertEqual(loads(stored["tags_json"]),tags)
        self.assertEqual(len(stub.calls),1)

    def test_malformed_unblocked_candidate_must_not_advance_cursor_or_disappear(self):
        valid={"blog_name":"first","id_string":"881",
               "post_url":"https://first.tumblr.com/post/881","timestamp":1001}
        broken={"blog_name":"first","id_string":"not-a-post-id",
                "post_url":"https://first.tumblr.com/post/bad","timestamp":1000}
        adapter,stub=self.scanner([envelope([valid,broken])])
        with self.assertRaisesRegex(ValueError,"discovery_page_post_unverified"):
            adapter.perform("tumblr.discover","tag:all-candidates",{})
        store=DiscoveryStore(self.root/"discovery.sqlite3")
        self.assertEqual(store.count_candidates("tag:all-candidates"),0)
        self.assertEqual(store.checkpoint("tag:all-candidates")["cursor"],0)
        self.assertEqual(len(stub.calls),1)

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
        os.chmod(dbpath,0o600)
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
                  {"type":"video","media":{"url":"https://64.media.tumblr.com/c.mp4"},"poster":[{"url":"https://64.media.tumblr.com/c.jpg"}]},
                  {"type":"image","media":[{"url":"https://evil.invalid/out.jpg"}]}]}
        adapter,_=self.scanner([envelope([post])])
        adapter.perform("tumblr.discover","tag:media",{})
        record=DiscoveryStore(self.root/"discovery.sqlite3").read_candidates()[0]
        import json
        refs=json.loads(record["media_json"])
        self.assertEqual([item["url"] for item in refs],[
            "https://64.media.tumblr.com/a.jpg",
            "https://64.media.tumblr.com/b.jpg",
            "https://64.media.tumblr.com/c.mp4",
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
