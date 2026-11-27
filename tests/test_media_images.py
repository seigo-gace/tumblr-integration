"""Images reuse the existing Library/MediaSave job ledger; no network calls."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.media_save import MediaSave, MediaError, _image_extension, MAX_IMAGE_BYTES


PNG=b"\x89PNG\r\n\x1a\n"+b"sample-image-fixture"

class ImageDownloader:
    def __init__(self,body=PNG):
        self.body=body
        self.calls=[]
    def probe(self,url):
        self.calls.append(("HEAD",url))
        return len(self.body)
    def fetch(self,url,path,size):
        self.calls.append(("GET",url))
        self_path=Path(path)
        self_path.write_bytes(self.body)
        return len(self.body)


class ForbiddenConverter:
    def convert(self,*args):
        raise AssertionError("image source must never reach ffmpeg")


class ImageSaveTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        os.chmod(self.root,0o700)
        self.store=DiscoveryStore(self.root/"discovery.sqlite3")
        self.store.save_page("tag:images",[
            {"name":"owner","url":"https://owner.tumblr.com"}
        ],[{
            "candidate_id":"owner:123","blog":"owner","post_id":"123",
            "url":"https://owner.tumblr.com/post/123",
            "title":"image","type":"image","date":"",
            "thumbnail_url":"","text_excerpt":"","tags":[],"note_count":0,
            "media_refs":[
                {"url":"https://64.media.tumblr.com/one.png","type":"image"},
                {"url":"https://64.media.tumblr.com/two.png","type":"image"},
            ]
        }],1,complete=True)
        self.media=self.root/"media"
        self.media.mkdir(mode=0o700)
        self.download=ImageDownloader()
        self.worker=MediaSave(self.store,self.media,transcoder=ForbiddenConverter(),
                              image_downloader=self.download)

    def test_image_is_saved_once_with_correct_extension_and_history(self):
        result=self.worker.save("owner:123",0,rights_confirmed=True)
        self.assertEqual(result["state"],"completed")
        self.assertEqual(result["method"],"direct-image")
        self.assertTrue(result["output_name"].endswith(".png"))
        saved=self.worker.file(result["job_id"])
        self.assertEqual(saved.read_bytes(),PNG)
        self.assertEqual(saved.stat().st_mode&0o077,0)
        self.assertEqual(len(self.download.calls),2)
        cached=self.worker.save("owner:123",0,True)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(len(self.download.calls),2)
        report=self.worker.list()
        self.assertEqual(report["jobs"][0]["source_method"],"direct-image")

    def test_explicit_rights_are_required_before_image_download(self):
        with self.assertRaisesRegex(MediaError,"MEDIA_RIGHTS_CONFIRMATION_REQUIRED"):
            self.worker.save("owner:123",1)
        self.assertFalse(self.download.calls)

    def test_invalid_image_signature_is_rejected_and_partial_cleaned(self):
        self.download.body=b"not an image"
        with self.assertRaisesRegex(MediaError,"MEDIA_IMAGE_FORMAT_INVALID"):
            self.worker.save("owner:123",0,True)
        self.assertEqual(self.worker.list()["jobs"][0]["state"],"failed")
        self.assertFalse(list(self.media.glob("*.part")))
        self.assertFalse(list(self.media.glob("*.png")))

    def test_all_supported_image_magic_formats_are_recognized(self):
        path=self.media/"bytes"
        for raw,extension in (
            (PNG,".png"),(b"\xff\xd8\xff\x00\x01",".jpg"),
            (b"GIF89a"+b"content",".gif"),
            (b"RIFF"+b"\x00"*4+b"WEBP"+b"x",".webp")):
            path.write_bytes(raw)
            self.assertEqual(_image_extension(path),extension)


if __name__=="__main__":
    unittest.main()
