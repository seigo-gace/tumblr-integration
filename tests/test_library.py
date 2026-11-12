"""Library/Viewer acceptance, using local private Discovery fixtures only.
No Tumblr or other external HTTP requests are made.
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.library import Library

class LibraryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder=Path(tmp.name)
        os.chmod(self.folder,0o700)
        self.store=DiscoveryStore(self.folder/"discovery.sqlite3")
        self.library=Library(self.store)
        posts=[]
        for number,blog in ((100,"alpha"),(101,"beta"),(102,"alpha")):
            posts.append({"candidate_id":f"{blog}:{number}","blog":blog,"post_id":str(number),
                "url":f"https://{blog}.tumblr.com/post/{number}","title":f"post {number}",
                "type":"image","date":"2026-11-11","thumbnail_url":"",
                "text_excerpt":"source text","tags":["topic"],"note_count":2,
                "media_refs":[{"url":f"https://64.media.tumblr.com/{number}.jpg","type":"image"},
                              {"url":f"https://64.media.tumblr.com/{number}b.jpg","type":"image"}]})
        self.store.save_page("tag:test",[
            {"name":"alpha","url":"https://alpha.tumblr.com"},
            {"name":"beta","url":"https://beta.tumblr.com"},
        ],posts,3,complete=True)

    def test_all_filter_unread_and_state_changes(self):
        all_items=self.library.list()
        self.assertEqual(all_items["total"],3)
        self.assertEqual(len(all_items["items"][0]["media"]),2)
        self.assertEqual(self.library.list("unread")["total"],3)
        result=self.library.update(["alpha:100","beta:101"],"seen",True)
        self.assertEqual(result["updated"],2)
        self.assertEqual(self.library.list("seen")["total"],2)
        self.assertEqual(self.library.list("unread")["total"],1)
        self.library.update(["beta:101"],"seen",False)
        self.assertEqual(self.library.list("unread")["total"],2)

    def test_favorites_later_and_discovery_refresh_preserves_choices(self):
        self.library.update(["alpha:100"],"favorite",True)
        self.library.update(["alpha:100"],"watch_later",True)
        self.store.save_page("tag:new",[],[{
            "candidate_id":"alpha:100","blog":"alpha","post_id":"100",
            "url":"https://alpha.tumblr.com/post/100","title":"updated",
            "type":"image","date":"2026-11-12","thumbnail_url":"",
            "text_excerpt":"","tags":[],"note_count":0,"media_refs":[],
        }],1,complete=True)
        favorites=self.library.list("favorite")
        self.assertEqual(favorites["total"],1)
        self.assertTrue(favorites["items"][0]["watch_later"])
        self.assertEqual(favorites["items"][0]["title"],"updated")
        self.assertEqual(self.library.list("watch_later")["total"],1)

    def test_gallery_progress_survives_restart_and_marks_seen(self):
        saved=self.library.progress("alpha:100",1)
        self.assertTrue(saved["verified"])
        fresh=Library(DiscoveryStore(self.store.path))
        state=fresh.list()
        self.assertEqual(state["last_position"],{"candidate_id":"alpha:100","media_index":1})
        self.assertEqual(state["resume_item"]["candidate_id"],"alpha:100")
        self.assertEqual(state["resume_item"]["last_media_index"],1)
        self.assertEqual(fresh.list(offset=2,limit=1)["resume_item"]["candidate_id"],"alpha:100")
        candidate=next(item for item in state["items"] if item["candidate_id"]=="alpha:100")
        self.assertEqual(candidate["last_media_index"],1)
        self.assertTrue(candidate["seen"])

    def test_unavailable_and_exclusion_no_destruction_of_favorites(self):
        self.library.update(["alpha:100"],"favorite",True)
        self.store.mark_unavailable("alpha:100")
        self.assertEqual(self.library.list("favorite")["total"],0)
        self.assertEqual(self.library.list("unavailable")["total"],1)
        self.store.exclude_blog("beta")
        self.assertEqual(self.library.list("all")["total"],1)
        self.assertEqual(self.library.list("unavailable")["total"],2)

    def test_cross_page_selection_and_atomic_150_item_bulk_update(self):
        posts=[{
            "candidate_id":f"alpha:{i}","blog":"alpha","post_id":str(i),
            "url":f"https://alpha.tumblr.com/post/{i}",
            "title":f"post {i}","type":"image","date":"",
            "thumbnail_url":"","text_excerpt":"","tags":[],"note_count":0,
            "media_refs":[{"url":f"https://64.media.tumblr.com/a{i}.jpg",
                           "type":"image"}],
        } for i in range(2000,2150)]
        self.store.save_page("tag:bulk",[],posts,150,complete=True)
        ids=[item["candidate_id"] for item in posts]
        selected=self.library.selected([ids[1],ids[119],ids[149]])
        self.assertEqual([item["candidate_id"] for item in selected["items"]],
                         [ids[1],ids[119],ids[149]])
        self.assertEqual(len(selected["items"][2]["media"]),1)
        self.assertEqual(self.library.update(ids,"favorite",True)["updated"],150)
        selected=self.library.selected([ids[149],ids[1]])
        self.assertTrue(all(item["favorite"] for item in selected["items"]))
        self.assertGreaterEqual(self.library.list("favorite")["total"],150)
        # The previous artificial 100000-offset ceiling must not hide
        # candidates; a real empty late page is safe and honest.
        self.assertEqual(self.library.list(offset=100001)["items"],[])
        self.store.exclude_blog("alpha")
        with self.assertRaisesRegex(ValueError,"library_candidate_unavailable"):
            self.library.selected([ids[0]])
        with self.assertRaisesRegex(ValueError,"library_candidate_unavailable"):
            self.library.update([ids[0],"beta:101"],"seen",True)
        self.assertFalse(self.library.selected(["beta:101"])["items"][0]["seen"])

    def test_invalid_updates_are_atomic_and_never_change_provider(self):
        for ids,field,value in [
            (["alpha:100","absent:555"],"seen",True),
            (["alpha:100","alpha:100"],"favorite",True),
            (["alpha:100"],"watch_later","yes"),
            (["alpha:100"],"deleted",True),
            (["alpha:100"],"seen",1),
        ]:
            with self.assertRaises(ValueError):
                self.library.update(ids,field,value)
        self.assertFalse(next(x for x in self.library.list()["items"] if x["candidate_id"]=="alpha:100")["seen"])
        self.assertEqual(self.library.list("favorite")["total"],0)

    def test_later_media_indexes_are_not_silently_truncated_and_resume(self):
        media=[{"url":f"https://64.media.tumblr.com/image-{i}.jpg","type":"image"}
               for i in range(85)]
        self.store.save_page("tag:many",[],[{
            "candidate_id":"alpha:140","blog":"alpha","post_id":"140",
            "url":"https://alpha.tumblr.com/post/140","title":"many images",
            "type":"image","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,"media_refs":media,
        }],4,complete=True)
        entry=next(x for x in self.library.list()["items"] if x["candidate_id"]=="alpha:140")
        self.assertEqual(len(entry["media"]),85)
        self.assertEqual(entry["media"][74]["url"],media[74]["url"])
        self.library.progress("alpha:140",74)
        reopened=Library(DiscoveryStore(self.store.path))
        position=reopened.list()["last_position"]
        self.assertEqual(position,{"candidate_id":"alpha:140","media_index":74})
        self.assertEqual(reopened.resume_item()["last_media_index"],74)
        with self.assertRaises(ValueError):
            reopened.progress("alpha:140",85)

    def test_all_700_media_refs_and_resume_position_699_survive(self):
        media=[{"url":f"https://64.media.tumblr.com/a-{i}.jpg","type":"image"}
               for i in range(700)]
        self.store.save_page("tag:complete",[],[{
            "candidate_id":"alpha:700","blog":"alpha","post_id":"700",
            "url":"https://alpha.tumblr.com/post/700","title":"all 700",
            "type":"image","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,"media_refs":media,
        }],700,complete=True)
        listed=self.library.list()
        record=next(x for x in listed["items"] if x["candidate_id"]=="alpha:700")
        self.assertEqual(len(record["media"]),700)
        self.assertEqual(record["media"][-1]["url"],media[-1]["url"])
        self.library.progress("alpha:700",699)
        loaded=Library(DiscoveryStore(self.store.path)).resume_item()
        self.assertEqual(loaded["last_media_index"],699)
        self.assertEqual(len(loaded["media"]),700)
        with self.assertRaises(ValueError):
            self.library.progress("alpha:700",700)

    def test_invalid_media_position_never_mutates(self):
        for index in (-1,2,50,True,"0"):
            with self.assertRaises(ValueError):
                self.library.progress("alpha:100",index)
        self.assertIsNone(self.library.list()["last_position"])

    def test_safely_rejects_non_tumblr_media_and_keeps_only_refs(self):
        self.store.save_page("tag:unsafe",[],[{
            "candidate_id":"alpha:103","blog":"alpha","post_id":"103",
            "url":"https://alpha.tumblr.com/post/103","title":"unsafe",
            "type":"video","date":"","thumbnail_url":"",
            "text_excerpt":"","tags":[],"note_count":0,"media_refs":[
                {"url":"https://attacker.invalid/p.jpg","type":"image"},
                {"url":"http://64.media.tumblr.com/p.jpg","type":"image"},
                {"url":"https://64.media.tumblr.com:bad/p.jpg","type":"image"},
                {"url":"https://64.media.tumblr.com/p.jpg","type":"image"},
                {"url":"https://64.media.tumblr.com/x","type":"exe"},
            ],
        }],1,complete=True)
        item=next(x for x in self.library.list()["items"] if x["candidate_id"]=="alpha:103")
        self.assertEqual(item["media"],[{"url":"https://64.media.tumblr.com/p.jpg","type":"image"}])

    def test_progress_indexes_match_only_safe_visible_media(self):
        self.store.save_page("tag:unsafe-index",[],[{
            "candidate_id":"alpha:104","blog":"alpha","post_id":"104",
            "url":"https://alpha.tumblr.com/post/104","title":"mixed references",
            "type":"image","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,"media_refs":[
                {"url":"https://attacker.invalid/hidden.jpg","type":"image"},
                {"url":"https://64.media.tumblr.com/first.jpg","type":"image"},
                {"url":"https://64.media.tumblr.com/second.jpg","type":"image"},
                {"url":"http://64.media.tumblr.com/hidden.jpg","type":"image"},
            ],
        }],1,complete=True)
        entry=next(x for x in self.library.list()["items"] if x["candidate_id"]=="alpha:104")
        self.assertEqual(len(entry["media"]),2)
        with self.assertRaisesRegex(ValueError,"library_media_index_invalid"):
            self.library.progress("alpha:104",2)
        self.assertIsNone(self.library.list()["last_position"])
        self.library.progress("alpha:104",1)
        resumed=Library(DiscoveryStore(self.store.path)).resume_item()
        self.assertEqual(resumed["last_media_index"],1)
        self.assertEqual(resumed["media"][1]["url"],"https://64.media.tumblr.com/second.jpg")

    def test_private_database_permissions_fail_closed(self):
        os.chmod(self.store.path,0o644)
        with self.assertRaises(ValueError):
            self.library.list()
        os.chmod(self.store.path,0o600)
        os.chmod(self.folder,0o755)
        with self.assertRaises(ValueError):
            self.library.list()

    def test_pagination_and_filter_validation(self):
        self.assertEqual(self.library.list(offset=1,limit=1)["total"],3)
        self.assertEqual(len(self.library.list(offset=1,limit=1)["items"]),1)
        for bad in ("unexpected",None):
            with self.assertRaises(ValueError):self.library.list(bad)
        with self.assertRaises(ValueError):self.library.list("all",offset=-1)
        with self.assertRaises(ValueError):self.library.list("all",limit=101)

if __name__=="__main__":
    unittest.main()
