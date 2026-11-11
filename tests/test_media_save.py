"""Media Save module acceptance: owner consent, bounded fetch, conversion, cleanup,
idempotency, durable result, and fail-closed. No external requests.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.media_save import (
    MediaSave,MediaError,_media_url,_safe_media,SafeDownloader,Transcoder,
    MAX_SOURCE_BYTES,MAX_TOTAL_SAVED_BYTES,
)

class FakeDownloader:
    def __init__(self,mode="ok"):
        self.mode=mode
        self.calls=[]
    def probe(self,url):
        self.calls.append(("HEAD",url))
        if self.mode=="no_head":raise MediaError("MEDIA_HTTP_UNAVAILABLE")
        if self.mode=="huge":return MAX_SOURCE_BYTES+1
        return 48
    def fetch(self,url,path,size):
        self.calls.append(("GET",url))
        with open(path,"xb") as f:f.write(b"fakedummy-video-data"*3)
        if self.mode=="partial":raise MediaError("MEDIA_SIZE_CHANGED")
        return size

class FakeTranscoder:
    def __init__(self,mode="ok"):
        self.calls=0
        self.mode=mode
    def convert(self,src,dst):
        self.calls+=1
        if self.mode=="failed":raise MediaError("MEDIA_CONVERSION_FAILED")
        dst.write_bytes(b"\x00\x00\x00\x14ftypisom"+b"X"*90)
        return dst.stat().st_size

class MediaSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        os.chmod(self.root,0o700)
        self.db=DiscoveryStore(self.root/"discovery.sqlite3")
        self.db.save_page("tag:test",[
            {"name":"source","url":"https://source.tumblr.com"},
        ],[
            {"candidate_id":"source:123","blog":"source","post_id":"123",
             "url":"https://source.tumblr.com/post/123","title":"sample",
             "type":"video","date":"","thumbnail_url":"","text_excerpt":"",
             "tags":[],"note_count":0,
             "media_refs":[{"url":"https://64.media.tumblr.com/sample.mp4","type":"video"},
                           {"url":"https://64.media.tumblr.com/poster.jpg","type":"image"}]},
        ],1,complete=True)
        self.media=self.root/"media"
        self.media.mkdir(mode=0o700)
        self.downloader=FakeDownloader()
        self.transcoder=FakeTranscoder()
        self.worker=MediaSave(self.db,self.media,self.downloader,self.transcoder)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "local FFmpeg/ffprobe not installed on this test runner")
    def test_real_offline_ffmpeg_probe_and_h264_mp4_conversion(self):
        src=self.media/"generated.avi"
        dest=self.media/"converted.mp4.part"
        cmd=["ffmpeg","-hide_banner","-loglevel","error","-nostdin","-y",
             "-f","lavfi","-i","color=c=blue:s=160x90:r=6:d=1",
             "-c:v","mpeg4","-f","avi",str(src)]
        subprocess.run(cmd,check=True,timeout=25)
        size=Transcoder().convert(src,dest)
        self.assertGreater(size,0)
        self.assertIn(b"ftyp",dest.read_bytes()[:32])
        self.assertEqual(Transcoder()._probe(dest)["video"],"h264")

    def test_all_135_saved_job_receipts_are_reachable_by_history_pages(self):
        from contextlib import closing
        with closing(self.worker._open()) as db:
            for n in range(135):
                db.execute("INSERT INTO media_save_jobs "
                    "(id,candidate_id,source_url,source_post_id,media_index,"
                    "state,created_at,updated_at) VALUES(?,?,?,?,?,'failed',?,?)",
                    (f"{n:032x}","source:123",
                     f"https://64.media.tumblr.com/history-{n}.mp4",
                     "123",n,n//2,n//2))
        first=self.worker.list(offset=0,limit=100)
        self.assertTrue(first["verified"])
        self.assertEqual(first["total"],135)
        self.assertTrue(first["has_next"])
        self.assertEqual(len(first["jobs"]),100)
        second=self.worker.list(offset=100,limit=100)
        self.assertEqual(second["total"],135)
        self.assertFalse(second["has_next"])
        self.assertEqual(len(second["jobs"]),35)
        self.assertEqual(len({job["id"] for job in first["jobs"]+second["jobs"]}),135)
        self.assertEqual(self.worker.list(offset=100001)["jobs"],[])
        for offset,limit in ((-1,100),(1.0,100),(0,0),(0,101)):
            with self.subTest(offset=offset,limit=limit):
                with self.assertRaises(MediaError):
                    self.worker.list(offset=offset,limit=limit)

    def test_saved_mp4_and_source_attribution_private(self):
        with self.assertRaisesRegex(MediaError,"MEDIA_RIGHTS_CONFIRMATION_REQUIRED"):
            self.worker.save("source:123",0)
        self.assertEqual(self.downloader.calls,[])
        result=self.worker.save("source:123",0,rights_confirmed=True)
        self.assertEqual(result["state"],"completed")
        self.assertEqual([x[0] for x in self.downloader.calls],["HEAD","GET"])
        jobs=self.worker.list()["jobs"]
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]["source_post_id"],"123")
        self.assertEqual(jobs[0]["source_bytes"],48)
        self.assertEqual(jobs[0]["state"],"completed")
        file=self.worker.file(result["job_id"])
        self.assertEqual(file.suffix,".mp4")
        self.assertEqual(file.stat().st_mode & 0o077,0)
        self.assertTrue(file.read_bytes().startswith(b"\x00\x00\x00\x14ftyp"))
        self.assertFalse(list(self.media.glob("*.part")))

    def test_repeated_request_is_cached_without_download(self):
        first=self.worker.save("source:123",0,True)
        second=self.worker.save("source:123",0,True)
        self.assertEqual(second["job"]["id"],first["job_id"])
        self.assertTrue(second["from_cache"])
        self.assertEqual(len(self.downloader.calls),2)
        self.assertEqual(self.transcoder.calls,1)

    def test_failed_conversion_cleans_partial_and_allows_explicit_retry(self):
        self.transcoder.mode="failed"
        with self.assertRaisesRegex(MediaError,"MEDIA_CONVERSION_FAILED"):
            self.worker.save("source:123",0,True)
        jobs=self.worker.list()["jobs"]
        self.assertEqual(jobs[0]["state"],"failed")
        self.assertEqual(jobs[0]["error_code"],"MEDIA_CONVERSION_FAILED")
        self.assertFalse(list(self.media.iterdir()))
        self.transcoder.mode="ok"
        done=self.worker.save("source:123",0,True)
        self.assertEqual(done["state"],"completed")
        self.assertEqual(len(self.worker.list()["jobs"]),1)

    def test_oversize_head_stops_before_get(self):
        self.downloader.mode="huge"
        with self.assertRaisesRegex(MediaError,"MEDIA_SIZE_EXCEEDED"):
            self.worker.save("source:123",0,True)
        self.assertEqual([x[0] for x in self.downloader.calls],["HEAD"])
        self.assertFalse(list(self.media.iterdir()))

    def test_partial_download_is_discarded(self):
        self.downloader.mode="partial"
        with self.assertRaisesRegex(MediaError,"MEDIA_SIZE_CHANGED"):
            self.worker.save("source:123",0,True)
        self.assertEqual(self.worker.list()["jobs"][0]["state"],"failed")
        self.assertFalse(list(self.media.iterdir()))

    def test_invalid_candidates_and_unavailable_do_not_fetch(self):
        for ident,idx in [("source:123",1),("source:123",True),("absent:456",0),("source:123",-1)]:
            with self.assertRaises(MediaError):
                self.worker.save(ident,idx,True)
        self.db.exclude_blog("source")
        with self.assertRaisesRegex(MediaError,"MEDIA_CANDIDATE_UNAVAILABLE"):
            self.worker.save("source:123",0,True)
        self.assertEqual(self.downloader.calls,[])

    def test_rejects_host_urls_and_credentialed_urls(self):
        for url in ("http://64.media.tumblr.com/vid.mp4",
                    "https://127.0.0.1/vid.mp4",
                    "https://evil.invalid/vid.mp4",
                    "https://media.tumblr.com:8080/vid.mp4",
                    "https://user@media.tumblr.com/vid.mp4",
                    "https://media.tumblr.com#fragment",
                    "https://media.tumblr.com:wrong/vid.mp4"):
            with self.assertRaises(MediaError):
                _media_url(url)

    def test_private_directory_rejects_unsafe_permissions(self):
        os.chmod(self.media,0o755)
        with self.assertRaisesRegex(MediaError,"MEDIA_DIRECTORY_UNSAFE"):
            self.worker.save("source:123",0,True)
        self.assertEqual(self.downloader.calls,[])

    def test_file_download_requires_valid_completed_id(self):
        for bad in ("../../secret","not-a-job","0"*32):
            with self.assertRaises(MediaError):
                self.worker.file(bad)

    def test_running_same_job_is_rejected_before_any_fetch(self):
        import sqlite3,time
        with self.worker._open() as db:
            db.execute("INSERT INTO media_save_jobs(id,candidate_id,source_url,source_post_id,"
                       "media_index,state,created_at,updated_at)"
                       " VALUES(?,?,?,?,?,'running',?,?)",(
                           "f"*32,"source:123","https://64.media.tumblr.com/sample.mp4",
                           "123",0,int(time.time()),int(time.time())))
        with self.assertRaisesRegex(MediaError,"MEDIA_JOB_IN_PROGRESS"):
            self.worker.save("source:123",0,True)
        self.assertEqual(self.downloader.calls,[])

if __name__=="__main__":
    unittest.main()
