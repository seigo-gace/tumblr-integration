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
        self.assertIn('env["TUMBLR_AI_LIVE_ENABLED"] = "1" if live_ai else "0"', text)
        self.assertIn('staged = start(STAGING_PORT, stage_release, token, root, live_ai=False)', text)
        self.assertIn('active = start(PRODUCTION_PORT, release, token, root, live_ai=ai_live)', text)
        self.assertIn('"127.0.0.1"', text)
        self.assertIn('TARGET_BRANCH = "feat/initial-modular-core"', text)
        self.assertIn('SOURCE_SHA_CHANGED', text)
        self.assertIn('OWNER_TOKEN_FILE_MISSING', text)
        self.assertNotIn("/proc/self/environ", text)
        self.assertNotIn("print(token", text)
        self.assertNotIn("generativelanguage.googleapis.com", text)

    def test_ai_live_opt_in_requires_existing_private_persistent_quota(self):
        import importlib.util
        import os
        import tempfile
        from pathlib import Path
        spec=importlib.util.spec_from_file_location("private_tumblr_release_contract",SCRIPT)
        release=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            private=root/"dist"/"data"
            private.mkdir(parents=True,mode=0o700)
            os.chmod(private,0o700)
            quota=private/"ai-quota.sqlite3"
            quota.write_bytes(b"")
            os.chmod(quota,0o600)
            opt={"TUMBLR_AI_LIVE_ENABLED":"1","TUMBLR_GEMINI_API_KEY":"not-real-key",
                 "TUMBLR_AI_QUOTA_DB":str(quota)}
            self.assertFalse(release.validated_live_ai(root,{}))
            self.assertTrue(release.validated_live_ai(root,opt))
            with self.assertRaisesRegex(release.ReleaseError,"AI_PROVIDER_KEY_NOT_CONFIGURED"):
                release.validated_live_ai(root,{k:v for k,v in opt.items() if k!="TUMBLR_GEMINI_API_KEY"})
            with self.assertRaisesRegex(release.ReleaseError,"AI_QUOTA_PERSISTENT_PATH_REQUIRED"):
                release.validated_live_ai(root,{k:v for k,v in opt.items() if k!="TUMBLR_AI_QUOTA_DB"})
            os.chmod(quota,0o644)
            with self.assertRaisesRegex(release.ReleaseError,"AI_QUOTA_PRIVATE_FILE_REQUIRED"):
                release.validated_live_ai(root,opt)
            os.chmod(quota,0o600)
            os.chmod(private,0o755)
            with self.assertRaisesRegex(release.ReleaseError,"AI_QUOTA_PRIVATE_FILE_REQUIRED"):
                release.validated_live_ai(root,opt)
            os.chmod(private,0o700)
            external=root/"outside.sqlite3"
            external.write_bytes(b"")
            os.chmod(external,0o600)
            with self.assertRaisesRegex(release.ReleaseError,"AI_QUOTA_PRIVATE_FILE_REQUIRED"):
                release.validated_live_ai(root,{**opt,"TUMBLR_AI_QUOTA_DB":str(external)})

    def test_staging_verifies_before_live_process_termination(self):
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(code.index("verify(STAGING_PORT"), code.index("os.kill(old_pid"))


if __name__ == "__main__":
    unittest.main()
