"""No live Tumblr traffic: verify rate limiting across repeated uses and restarts."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tumblrkit.api_safety import TumblrReadBudget
from tumblrkit.tumblr_read import TumblrReadAdapter


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        os.chmod(self.folder, 0o700)
        self.clock = [1800000000]
        self.path = self.folder / "tumblr-api-budget.sqlite3"

    def budget(self):
        return TumblrReadBudget(self.path, clock=lambda: self.clock[0])

    def test_private_file_and_persistent_cooldown(self):
        self.budget().reserve()
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)
        with self.assertRaisesRegex(ValueError, "tumblr_budget_cooldown"):
            self.budget().reserve()
        self.clock[0] += 61
        self.budget().reserve()

    def test_hourly_limit_fails_closed(self):
        for n in range(6):
            self.clock[0] += 61
            self.budget().reserve()
        self.clock[0] += 61
        with self.assertRaisesRegex(ValueError, "tumblr_budget_exhausted"):
            self.budget().reserve()

    def test_429_pauses_across_restart(self):
        self.budget().reserve()
        self.budget().pause_after_limit()
        self.clock[0] += 300
        with self.assertRaisesRegex(ValueError, "tumblr_budget_cooldown"):
            self.budget().reserve()

    def test_unsafe_directory_or_symlink_fails(self):
        os.chmod(self.folder, 0o755)
        with self.assertRaises(ValueError):
            self.budget().reserve()
        os.chmod(self.folder, 0o700)
        marker = self.folder / "marker"
        marker.write_text("unsafe")
        self.path.symlink_to(marker)
        with self.assertRaises(ValueError):
            self.budget().reserve()

    def test_real_adapter_blocks_before_any_network_call(self):
        token = self.folder / "access-token"
        token.write_text("testing-access-token-not-real")
        os.chmod(token, 0o600)
        adapter = TumblrReadAdapter("myblog", token)
        adapter.transport.budget = self.budget()
        adapter.transport.budget.reserve()
        with patch("tumblrkit.tumblr_read.urlopen") as opened:
            with self.assertRaisesRegex(ValueError, "tumblr_budget_cooldown"):
                adapter.perform("tumblr.list_posts", "myblog", {})
            opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
