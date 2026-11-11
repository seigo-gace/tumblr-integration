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

    def test_explicit_stopped_process_recovery_guard(self):
        import importlib.util
        import tempfile
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location("private_tumblr_release_recovery",SCRIPT)
        release=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(Path,"iterdir",return_value=[]), patch.object(
                release,"wait_port",return_value=False
            ) as port:
                with self.assertRaisesRegex(release.ReleaseError,"OWNER_PROCESS_NOT_UNIQUE"):
                    release.find_old(root)
                self.assertIsNone(release.find_old(root,allow_stopped=True))
                port.assert_called_with(release.PRODUCTION_PORT,True,1)
            with patch.object(Path,"iterdir",return_value=[]), patch.object(
                release,"wait_port",return_value=True
            ):
                with self.assertRaisesRegex(release.ReleaseError,"UNKNOWN_PRODUCTION_LISTENER"):
                    release.find_old(root,allow_stopped=True)
        code=SCRIPT.read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--recover-stopped", action="store_true"',code)
        self.assertIn("old_pid = find_old(root, allow_stopped=args.recover_stopped)",code)
        self.assertIn("if old_pid is not None:",code)

    def test_process_selection_does_not_count_staging_as_production(self):
        import importlib.util
        from unittest.mock import patch
        spec=importlib.util.spec_from_file_location("private_tumblr_release_processes",SCRIPT)
        release=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        base=["python3","-B","-m","tumblrkit.appserve","serve"]
        self.assertTrue(release._production_process_args(base+["--port","8081"]))
        self.assertTrue(release._production_process_args(base+["--port=8081"]))
        self.assertFalse(release._production_process_args(base+["--port","8082"]))
        self.assertFalse(release._production_process_args(base+["--port=8082"]))
        self.assertTrue(release._production_process_args(base))
        self.assertTrue(release._production_process_args(base+["--port","invalid"]))
        self.assertTrue(release._production_process_args(base+["--port"]))
        # Simulated /proc verifies the process is not selected merely because
        # it shares the production checkout directory.
        class Entry:
            def __init__(self, name, root, argv):
                self.name=name
                self.root=root
                self.argv=argv
            def __truediv__(self, name):
                parent=self
                class Part:
                    def read_bytes(self):
                        return "\0".join(parent.argv).encode()+b"\0"
                    def resolve(self):
                        return parent.root
                return Part()
        from pathlib import Path
        with patch.object(Path,"iterdir",return_value=[
            Entry("101",Path("/tmp/tumblr-checkout"),base+["--port","8081"]),
            Entry("202",Path("/tmp/tumblr-checkout"),base+["--port","8082"]),
        ]):
            self.assertEqual(release.find_old(Path("/tmp/tumblr-checkout")),101)

    def test_staging_verifies_before_live_process_termination(self):
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertLess(code.index("verify(STAGING_PORT"), code.index("os.kill(old_pid"))


if __name__ == "__main__":
    unittest.main()
