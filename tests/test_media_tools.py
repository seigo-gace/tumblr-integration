"""No-download safety checks for the opt-in app-private FFmpeg installer."""
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import tumblr_media_tools as installer

class FakeResponse:
    def __init__(self,data,url):
        self.source=io.BytesIO(data)
        self.url=url
        self.status=200
    def read(self,size):return self.source.read(size)
    def __enter__(self):return self
    def __exit__(self,*args):return False

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)
        os.chmod(self.folder,0o700)

    def test_pinned_sources_are_complete_sha256_and_arch_supported(self):
        self.assertEqual(len(installer.SOURCE_COMMIT),40)
        for key,(label,digest,size) in installer.SOURCES.items():
            self.assertIn(label,("linux","linux_arm64"))
            self.assertEqual(len(digest),64)
            self.assertGreater(size,1000000)

    def test_unexpected_download_bytes_rejected_before_execution(self):
        fake=b"not-a-verified-binary"
        with patch("scripts.tumblr_media_tools.platform.system",return_value="Linux"),\
             patch("scripts.tumblr_media_tools.platform.machine",return_value="x86_64"),\
             patch("scripts.tumblr_media_tools.urllib.request.urlopen",return_value=FakeResponse(fake,"https://media.githubusercontent.com/mock")):
            with self.assertRaisesRegex(installer.InstallError,"TOOLS_HASH_MISMATCH"):
                installer.install(self.folder/"media-tools")
        self.assertFalse((self.folder/"media-tools"/"ffmpeg").exists())
        self.assertFalse((self.folder/"media-tools"/"ffprobe").exists())

    def test_untrusted_directory_never_downloads(self):
        unsafe=self.folder/"unsafe"
        unsafe.mkdir(mode=0o700)
        os.chmod(unsafe,0o755)
        with self.assertRaisesRegex(installer.InstallError,"TOOLS_DIRECTORY_UNSAFE"):
            installer.install(unsafe)

    def test_symlink_directory_never_downloads(self):
        link=self.folder/"link"
        link.symlink_to(self.folder,target_is_directory=True)
        with self.assertRaisesRegex(installer.InstallError,"TOOLS_DIRECTORY_UNSAFE"):
            installer.install(link)

    def test_non_linux_platform_refused(self):
        with patch("scripts.tumblr_media_tools.platform.system",return_value="Windows"):
            with self.assertRaisesRegex(installer.InstallError,"TOOLS_PLATFORM_UNSUPPORTED"):
                installer.install(self.folder/"media-tools")

if __name__=="__main__":
    unittest.main()
