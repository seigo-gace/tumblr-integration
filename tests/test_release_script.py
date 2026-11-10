"""Offline structural checks for the manual bounded release script.

Does not execute deployment, rotate keys, restart services or call providers.
"""
import ast
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tumblr_release.py"


class ReleaseScriptContract(unittest.TestCase):
    def test_script_compiles_without_running(self):
        compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")

    def test_only_owned_app_and_local_loopback(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("TUMBLR_APP_ACCESS_TOKEN", text)
        self.assertIn('env["TUMBLR_AI_LIVE_ENABLED"] = "0"', text)
        self.assertIn('"127.0.0.1"', text)
        self.assertIn('TARGET_BRANCH = "feat/initial-modular-core"', text)
        self.assertIn('SOURCE_SHA_CHANGED', text)
        self.assertIn('OWNER_TOKEN_FILE_MISSING', text)
        self.assertNotIn("/proc/self/environ", text)
        self.assertNotIn("print(token", text)
        self.assertNotIn("generativelanguage.googleapis.com", text)

    def test_staging_verifies_before_live_process_termination(self):
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(code.index("verify(STAGING_PORT"), code.index("os.kill(old_pid"))


if __name__ == "__main__":
    unittest.main()
