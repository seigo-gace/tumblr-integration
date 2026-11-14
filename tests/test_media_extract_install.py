"""Pinned extractor installer safety checks; never contact PyPI/Tumblr in CI."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from scripts.tumblr_media_extractors import install,InstallerError,WHEELS

class InstallerSafetyTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        os.chmod(self.root,0o700)

    def test_two_exact_pypi_wheels_locked(self):
        self.assertEqual(len(WHEELS),2)
        self.assertEqual({name.split("-")[0] for name,sha in WHEELS},
                         {"gallery_dl","yt_dlp"})
        for filename,digest in WHEELS:
            self.assertTrue(filename.endswith(".whl"))
            self.assertEqual(len(digest),64)
            self.assertTrue(all(c in "0123456789abcdef" for c in digest))

    def test_rejects_world_readable_application_folder(self):
        os.chmod(self.root,0o755)
        with self.assertRaisesRegex(InstallerError,"APP_DATA_UNSAFE"):
            install(self.root)

    def test_rejects_symlink_application_folder(self):
        link=self.root.parent/(self.root.name+"-link")
        link.symlink_to(self.root,target_is_directory=True)
        try:
            with self.assertRaisesRegex(InstallerError,"APP_DATA_UNSAFE"):
                install(link)
        finally:
            link.unlink()

    def test_partial_install_does_not_bypass_verified_state(self):
        media=self.root/"media-extractors"
        media.mkdir(mode=0o700)
        with self.assertRaisesRegex(InstallerError,"EXTRACTOR_PARTIAL_INSTALL"):
            install(self.root)

if __name__=="__main__":
    unittest.main()
