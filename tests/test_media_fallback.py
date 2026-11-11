"""Adopted Media Save fallback acceptance: offline only, no Tumblr requests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.media_save import MediaSave,MediaError
from tumblrkit.media_extract import (
    ExtractorFallback,ExtractionFailure,verified_post_url,
)

class DummyConverter:
    def convert(self,src,dst):
        assert src.is_file() and src.stat().st_size>0
        dst.write_bytes(b"\x00\x00\x00\x14ftypisom"+b"A"*80)
        return dst.stat().st_size

class DummyFallback:
    def __init__(self,mode="gallery-dl"):
        self.calls=[]
        self.mode=mode
    def fetch(self,post_url,blog,post_id,destination):
        self.calls.append((post_url,blog,post_id))
        if self.mode=="denied":
            raise ExtractionFailure("MEDIA_PROVIDER_REFUSED")
        if self.mode=="missing":
            raise ExtractionFailure("MEDIA_ALL_METHODS_EXHAUSTED")
        Path(destination).write_bytes(b"local-video-data"*4)
        return {"bytes":Path(destination).stat().st_size,"method":self.mode}

class MissingDirect:
    def __init__(self,code="MEDIA_HTTP_UNAVAILABLE"):
        self.calls=[]
        self.code=code
    def probe(self,url):
        self.calls.append("HEAD")
        raise MediaError(self.code)
    def fetch(self,*args):
        self.calls.append("GET")
        raise AssertionError("should not download")

class MediaFallbackTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder=Path(tmp.name)
        os.chmod(self.folder,0o700)
        self.store=DiscoveryStore(self.folder/"discovery.sqlite3")
        self.media=self.folder/"media"
        self.media.mkdir(mode=0o700)
    def add_post(self,direct=False,post_type="video"):
        self.store.save_page("tag:test",[{"name":"one","url":"https://one.tumblr.com"}],[{
            "candidate_id":"one:123","blog":"one","post_id":"123",
            "url":"https://one.tumblr.com/post/123","title":"saved post",
            "type":post_type,"date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,
            "media_refs":([{"url":"https://64.media.tumblr.com/a.mp4","type":"video"}] if direct else [])
        }],1,complete=True)

    def worker(self,extractor=None,downloader=None):
        return MediaSave(self.store,self.media,downloader or MissingDirect(),
                         DummyConverter(),extractor or DummyFallback())

    def test_discovery_detects_npf_video_without_direct_media_url(self):
        from tumblrkit.discovery import DiscoveryEngine
        post={"blog_name":"one","id_string":"123",
              "post_url":"https://one.tumblr.com/post/123",
              "type":"text","summary":"video embedded",
              "content":[{"type":"video","poster":[{"url":"https://64.media.tumblr.com/poster.jpg"}]}]}
        candidate=DiscoveryEngine._post(post,set())
        self.assertEqual(candidate["type"],"video")
        self.assertEqual(candidate["media_refs"][0]["type"],"image")

    def test_many_npf_renditions_are_preserved_without_silent_50_ref_drop(self):
        from tumblrkit.tumblr_read import discovery_media_refs,MAX_DISCOVERY_MEDIA_REFS
        blocks=[]
        for image in range(25):
            blocks.append({"type":"image","media":[
                {"url":f"https://64.media.tumblr.com/{image}-{size}.jpg"}
                for size in range(4)]})
        refs=discovery_media_refs({"content":blocks})
        self.assertEqual(len(refs),100)
        self.assertEqual(refs[99]["url"],"https://64.media.tumblr.com/24-3.jpg")
        self.assertGreater(MAX_DISCOVERY_MEDIA_REFS,100)
        overflow={"content":[{"type":"image","media":[
            {"url":f"https://64.media.tumblr.com/p-{index}.jpg"}
            for index in range(MAX_DISCOVERY_MEDIA_REFS+1)]}]}
        with self.assertRaisesRegex(ValueError,"discovery_media_refs_limit_exceeded"):
            discovery_media_refs(overflow)

    def test_video_selection_above_previous_index_49_remains_available(self):
        self.add_post(False)
        refs=[{"url":f"https://64.media.tumblr.com/photo-{i}.jpg","type":"image"}
              for i in range(60)]
        refs.append({"url":"https://64.media.tumblr.com/late-video.mp4","type":"video"})
        with self.store._open() as db:
            import json
            db.execute("UPDATE discovery_posts SET media_json=? WHERE candidate_id=?",
                       (json.dumps(refs),"one:123"))
            picked=self.worker()._candidate(db,"one:123",60)
        self.assertEqual(picked[0],"123")
        self.assertEqual(picked[1],"https://64.media.tumblr.com/late-video.mp4")
        self.assertTrue(picked[4])
        with self.store._open() as db:
            with self.assertRaisesRegex(MediaError,"MEDIA_INDEX_INVALID"):
                self.worker()._candidate(db,"one:123",len(refs)+500)

    def test_legacy_tumblr_video_url_is_preserved_as_video(self):
        from tumblrkit.tumblr_read import discovery_media_refs
        post={"video_url":"https://64.media.tumblr.com/legacy.mp4",
              "thumbnail_url":"https://64.media.tumblr.com/poster.jpg"}
        self.assertEqual(discovery_media_refs(post),[
            {"url":"https://64.media.tumblr.com/legacy.mp4","type":"video"},
            {"url":"https://64.media.tumblr.com/poster.jpg","type":"image"}])

    def test_without_direct_url_uses_verified_post_gallery_method(self):
        self.add_post(False)
        extractor=DummyFallback()
        direct=MissingDirect()
        worker=self.worker(extractor,direct)
        result=worker.save("one:123",0,True)
        self.assertEqual(result["method"],"gallery-dl")
        self.assertEqual(extractor.calls,[("https://one.tumblr.com/post/123","one","123")])
        self.assertEqual(direct.calls,[])
        self.assertEqual(worker.list()["jobs"][0]["source_method"],"gallery-dl")
        self.assertEqual(worker.list()["jobs"][0]["source_post_id"],"123")
        self.assertTrue(worker.file(result["job_id"]).is_file())

    def test_video_block_without_direct_source_uses_post_fallback(self):
        self.add_post(False)
        with self.store._open() as db:
            db.execute("UPDATE discovery_posts SET media_json=? WHERE candidate_id=?",
                       ('[{"type":"video","poster":"https://64.media.tumblr.com/poster.jpg"}]',
                        "one:123"))
        extractor=DummyFallback()
        result=self.worker(extractor).save("one:123",0,True)
        self.assertEqual(result["method"],"gallery-dl")
        self.assertEqual(extractor.calls,[("https://one.tumblr.com/post/123","one","123")])

    def test_invalid_video_source_must_not_bypass_url_guard(self):
        self.add_post(False)
        with self.store._open() as db:
            db.execute("UPDATE discovery_posts SET media_json=? WHERE candidate_id=?",
                       ('[{"type":"video","url":"https://attacker.invalid/source.mp4"}]',
                        "one:123"))
        extractor=DummyFallback()
        with self.assertRaisesRegex(MediaError,"MEDIA_SELECTION_INVALID"):
            self.worker(extractor).save("one:123",0,True)
        self.assertEqual(extractor.calls,[])

    def test_video_metadata_fallback_rejects_ambiguous_selections(self):
        self.add_post(False)
        with self.store._open() as db:
            db.execute("UPDATE discovery_posts SET media_json=? WHERE candidate_id=?",
                       ('[{"type":"image","url":"https://64.media.tumblr.com/poster.jpg"},'
                        '{"type":"video","poster":"unused"}]',"one:123"))
        extractor=DummyFallback()
        with self.assertRaisesRegex(MediaError,"MEDIA_SELECTION_INVALID"):
            self.worker(extractor).save("one:123",0,True)
        self.assertEqual(extractor.calls,[])

    def test_direct_not_available_uses_ytdlp_and_preserves_post_identity(self):
        self.add_post(True)
        extractor=DummyFallback("yt-dlp")
        worker=self.worker(extractor)
        result=worker.save("one:123",0,True)
        self.assertEqual(result["method"],"yt-dlp")
        self.assertEqual(worker.list()["jobs"][0]["source_method"],"yt-dlp")
        self.assertEqual(len(extractor.calls),1)
        cached=worker.save("one:123",0,True)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(len(extractor.calls),1)

    def test_explicit_provider_denial_never_attempts_fallback(self):
        self.add_post(True)
        extractor=DummyFallback()
        direct=MissingDirect("MEDIA_PROVIDER_REFUSED")
        with self.assertRaisesRegex(MediaError,"MEDIA_PROVIDER_REFUSED"):
            self.worker(extractor,direct).save("one:123",0,True)
        self.assertEqual(extractor.calls,[])
        self.assertEqual(direct.calls,["HEAD"])

    def test_extractor_refusal_is_not_retried(self):
        self.add_post(False)
        extractor=DummyFallback("denied")
        worker=self.worker(extractor)
        with self.assertRaisesRegex(MediaError,"MEDIA_PROVIDER_REFUSED"):
            worker.save("one:123",0,True)
        self.assertEqual(len(extractor.calls),1)
        self.assertEqual(worker.list()["jobs"][0]["state"],"failed")
        self.assertFalse(list(self.media.iterdir()))

    def test_non_video_and_unknown_id_never_extract(self):
        self.add_post(False,post_type="image")
        extractor=DummyFallback()
        with self.assertRaisesRegex(MediaError,"MEDIA_SELECTION_INVALID"):
            self.worker(extractor).save("one:123",0,True)
        self.assertEqual(extractor.calls,[])

    def test_malformed_post_or_mismatched_id_blocked(self):
        for url in ("https://one.tumblr.com/post/124",
                    "https://attacker.invalid/post/123",
                    "https://one.tumblr.com/post/123?redirect=https://evil.invalid",
                    "http://one.tumblr.com/post/123",
                    "https://one.tumblr.com/post/1234"):
            with self.assertRaisesRegex(ExtractionFailure,"MEDIA_POST_INVALID"):
                verified_post_url(url,"one","123")
        self.assertEqual(verified_post_url("https://www.tumblr.com/one/123","one","123"),
                         "https://www.tumblr.com/one/123")

    def test_no_extractor_makes_no_external_request(self):
        fallback=ExtractorFallback(self.folder/"uninstalled"/"bin")
        with self.assertRaisesRegex(ExtractionFailure,"MEDIA_ALL_METHODS_EXHAUSTED"):
            fallback.fetch("https://one.tumblr.com/post/123","one","123",self.media/"one.source.part")

    def test_real_extractor_dispatch_is_sequential_and_stops_on_refusal(self):
        fallback=ExtractorFallback(self.folder/"mock-tool"/"bin")
        calls=[]
        def run(name,url,folder):
            calls.append(name)
            if name=="gallery-dl":raise ExtractionFailure("MEDIA_EXTRACTOR_NOT_FOUND")
            video=Path(folder)/"video.mp4"
            video.write_bytes(b"safe-video-test")
            return video
        with patch.object(fallback,"_run",side_effect=run):
            out=fallback.fetch("https://one.tumblr.com/post/123","one","123",
                               self.media/"out.source.part")
        self.assertEqual(calls,["gallery-dl","yt-dlp"])
        self.assertEqual(out["method"],"yt-dlp")
        self.assertTrue((self.media/"out.source.part").is_file())
        calls.clear()
        def blocked(name,url,folder):
            calls.append(name)
            raise ExtractionFailure("MEDIA_PROVIDER_REFUSED")
        with patch.object(fallback,"_run",side_effect=blocked):
            with self.assertRaisesRegex(ExtractionFailure,"MEDIA_PROVIDER_REFUSED"):
                fallback.fetch("https://one.tumblr.com/post/123","one","123",
                               self.media/"denied.source.part")
        self.assertEqual(calls,["gallery-dl"])

if __name__=="__main__":
    unittest.main()
