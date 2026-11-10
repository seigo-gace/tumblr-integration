"""Offline structural checks for the manual bounded release script.

Does not execute deployment, rotate keys, restart services or call providers.
"""
import ast
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "tumblr_release.py"
OWNER_SETUP_SCRIPT = SCRIPT.with_name("tumblr_owner_setup.py")


class ReleaseScriptContract(unittest.TestCase):
    def test_script_compiles_without_running(self):
        compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec")
        compile(OWNER_SETUP_SCRIPT.read_text(encoding="utf-8"), str(OWNER_SETUP_SCRIPT), "exec")

    def test_login_key_minimum_eight_and_direct_script_import(self):
        from tumblrkit.appserve import require_token
        self.assertEqual("abc12345", require_token("abc12345"))
        from tumblrkit.appserve import ServeError
        with self.assertRaises(ServeError):
            require_token("abc1234")
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("sys.path.insert(0, str(Path(__file__).resolve().parents[1]))", code)
        html = (SCRIPT.parents[1] / "tumblrkit" / "appserve.py").read_text(encoding="utf-8")
        self.assertIn('minlength="8"', html)

    def test_owner_key_setup_local_and_private(self):
        text = OWNER_SETUP_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("getpass.getpass(", text)
        self.assertIn("0o600", text)
        self.assertIn("tumblr-app-access-token", text)
        self.assertNotIn("print(value", text)

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
