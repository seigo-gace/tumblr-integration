import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tumblrkit.account_runtime import OwnerOAuth
from tumblrkit.oauth import TOKEN, USER_INFO


class FakeTransport:
    def __init__(self, blogs=("recruitment",)):
        self.blogs = blogs
        self.posts = []
        self.reads = []
    def post_form(self, url, data):
        self.posts.append((url, dict(data)))
        if data["grant_type"] == "refresh_token":
            return {"access_token": "new-access-token-123", "refresh_token": "new-refresh-token-456",
                    "token_type": "bearer", "expires_in": 3600}
        return {"access_token": "initial-access-token-123", "refresh_token": "initial-refresh-token-456",
                "token_type": "bearer", "expires_in": 3600}
    def get_json(self, url, token):
        self.reads.append((url, token))
        return {"response": {"user": {"blogs": [{"name": name} for name in self.blogs]}}}


class OwnerAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        for file, value in (("consumer-key", "a-valid-consumer-key"),
                            ("client-secret", "a-valid-client-secret")):
            p = self.root / file
            p.write_text(value)
            os.chmod(p, 0o600)
        self.transport = FakeTransport()
        self.oauth = OwnerOAuth(self.root, "recruitment", self.transport)

    def test_blog_setup_is_private_and_locks_after_connection(self):
        from tumblrkit.account_runtime import register_owner_blog, load_owner_blog
        self.assertEqual(register_owner_blog(self.root, "recruitment.tumblr.com"), "recruitment")
        self.assertEqual(load_owner_blog(self.root), "recruitment")
        self.assertEqual((self.root / "owner-blog").stat().st_mode & 0o077, 0)
        state = parse_qs(urlsplit(self.oauth.start()).query)["state"][0]
        self.oauth.callback("state=" + state + "&code=valid-code")
        with self.assertRaises(ValueError):
            register_owner_blog(self.root, "private-personal")

    def test_authorization_owner_and_refresh_lifecycle(self):
        url = self.oauth.start()
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params["scope"], ["basic offline_access"])
        self.assertEqual(params["redirect_uri"], ["https://tumblr.hentaigay.org/oauth/callback"])
        self.assertNotIn("a-valid-client-secret", url)
        result = self.oauth.callback("state=" + params["state"][0] + "&code=valid-code")
        self.assertEqual(result["blog"], "recruitment")
        self.assertEqual(self.oauth.status()["connected"], True)
        self.assertEqual(self.oauth.refresh_if_needed(), "initial-access-token-123")
        record = self.oauth.store.read()
        record["expires_at"] = int(time.time()) - 1
        self.oauth.store._atomic(self.oauth.store.tokens, json.dumps(record))
        self.assertEqual(self.oauth.refresh_if_needed(), "new-access-token-123")
        self.assertEqual(self.oauth.store.access.read_text().strip(), "new-access-token-123")
        self.assertEqual(self.transport.posts[1][0], TOKEN)
        self.assertEqual(self.transport.reads[0][0], USER_INFO)
        for name in ("account-tokens.json", "access-token"):
            self.assertEqual((self.root / name).stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            self.oauth.callback("state=" + params["state"][0] + "&code=again")

    def test_wrong_blog_never_persists(self):
        self.transport.blogs = ("personal-private",)
        state = parse_qs(urlsplit(self.oauth.start()).query)["state"][0]
        with self.assertRaises(ValueError):
            self.oauth.callback("state=" + state + "&code=valid-code")
        self.assertFalse(self.oauth.store.access.exists())
        self.assertFalse(self.oauth.store.tokens.exists())

    def test_invalid_state_never_calls_provider(self):
        self.oauth.start()
        with self.assertRaises(ValueError):
            self.oauth.callback("state=unknown&code=valid-code")
        self.assertEqual(self.transport.posts, [])

    def test_secret_permissions_rejected(self):
        os.chmod(self.root / "client-secret", 0o644)
        with self.assertRaises(ValueError):
            self.oauth.start()

    def test_refresh_requires_saved_refresh_token(self):
        state = parse_qs(urlsplit(self.oauth.start()).query)["state"][0]
        self.oauth.callback("state=" + state + "&code=valid-code")
        record = self.oauth.store.read()
        record["refresh_token"] = ""
        record["expires_at"] = int(time.time()) - 1
        self.oauth.store._atomic(self.oauth.store.tokens, json.dumps(record))
        with self.assertRaises(ValueError):
            self.oauth.refresh_if_needed()


if __name__ == "__main__":
    unittest.main()
