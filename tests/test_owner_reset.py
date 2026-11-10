"""Offline key reset tests; no process restart and no public API call."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from tumblrkit import appserve
from scripts import tumblr_owner_setup as owner


class OwnerResetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.token_file = self.folder / "tumblr-app-access-token"
        self.token_file.write_text("original-key-1234\n", encoding="utf-8")
        self.token_file.chmod(0o600)

    def test_reset_requires_eight_chars(self):
        with patch("getpass.getpass", side_effect=["newkey88", "newkey88"]):
            self.assertEqual(owner.ask_new_token(), "newkey88")
        with patch("getpass.getpass", side_effect=["short7", "short7"]):
            with self.assertRaises(appserve.ServeError):
                owner.ask_new_token()

    def test_failed_release_preserves_existing_key(self):
        with patch("getpass.getpass", side_effect=["newkey88", "newkey88"]):
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=1)):
                with self.assertRaises(owner.SetupError):
                    owner.reset_token(self.token_file, self.folder, "a" * 40)
        self.assertEqual(self.token_file.read_text(), "original-key-1234\n")
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_successful_release_replaces_existing_key(self):
        def fake_release(argv, **kwargs):
            pending = Path(argv[argv.index("--token-file") + 1])
            self.assertEqual(pending.read_text(), "newkey88\n")
            self.assertEqual(pending.stat().st_mode & 0o077, 0)
            self.assertEqual(self.token_file.read_text(), "original-key-1234\n")
            return SimpleNamespace(returncode=0)
        with patch("getpass.getpass", side_effect=["newkey88", "newkey88"]):
            with patch("subprocess.run", side_effect=fake_release):
                owner.reset_token(self.token_file, self.folder, "a" * 40)
        self.assertEqual(self.token_file.read_text(), "newkey88\n")
        self.assertEqual(self.token_file.stat().st_mode & 0o077, 0)
        self.assertEqual(len(list(self.folder.iterdir())), 1)

    def test_wrong_confirmation_does_not_modify_file(self):
        with patch("getpass.getpass", side_effect=["newkey88", "different8"]):
            with self.assertRaises(owner.SetupError):
                owner.reset_token(self.token_file, self.folder, "a" * 40)
        self.assertEqual(self.token_file.read_text(), "original-key-1234\n")


if __name__ == "__main__":
    unittest.main()
