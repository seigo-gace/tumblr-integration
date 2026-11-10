"""Offline verification of the official Tumblr read-only posts bridge."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.tumblr_read import TumblrReadAdapter, adapter_from_env, normalize_blog


class StubTransport:
    def __init__(self, owner=True):
        self.calls = []
        self.owner = owner

    def get(self, path, bearer):
        self.calls.append(path)
        if path == "/v2/user/info":
            return {"response": {"user": {"blogs": [
                {"name": "myblog" if self.owner else "another"}
            ]}}}
        return {"meta": {"status": 200}, "response": {
            "blog": {"name": "myblog"},
            "posts": [
                {"summary": "動画のタイトル", "type": "video",
                 "date": "2026-11-11 00:00:00 GMT",
                 "post_url": "https://myblog.tumblr.com/post/123"}
            ],
        }}


class TumblrReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.token = Path(self.temp.name) / "oauth-token"
        self.token.write_text("example-nonreal-oauth-token", encoding="utf-8")
        os.chmod(self.token, 0o600)

    def test_readonly_posts_from_verified_owner(self):
        transport = StubTransport()
        reader = TumblrReadAdapter("myblog", str(self.token), transport=transport)
        assistant = Assistant(bridge=Bridge({"tumblr": reader}), providers=[])
        result = assistant.turn("投稿一覧を見せて 対象 myblog")
        self.assertEqual("completed", result["state"])
        self.assertTrue(result["executed"])
        self.assertEqual("動画のタイトル", result["view"]["rows"][0]["title"])
        self.assertEqual("https://myblog.tumblr.com/post/123", result["view"]["rows"][0]["url"])
        self.assertEqual(2, len(transport.calls))
        self.assertEqual("/v2/user/info", transport.calls[0])

    def test_other_blog_and_unverified_owner_fail_closed(self):
        transport = StubTransport()
        reader = TumblrReadAdapter("myblog", str(self.token), transport=transport)
        result = Bridge({"tumblr": reader}).dispatch(__import__(
            "tumblrkit.bridge", fromlist=["ActionRequest"]
        ).ActionRequest("x1", "tumblr.list_posts", "another"))
        self.assertEqual("failed", result.state)
        self.assertEqual([], transport.calls)
        rejected = Bridge({"tumblr": TumblrReadAdapter(
            "myblog", str(self.token), StubTransport(owner=False)
        )}).dispatch(__import__(
            "tumblrkit.bridge", fromlist=["ActionRequest"]
        ).ActionRequest("x2", "tumblr.list_posts", "myblog"))
        self.assertEqual("failed", rejected.state)

    def test_unconfigured_and_insecure_token_file_not_connected(self):
        self.assertIsNone(adapter_from_env({}))
        self.assertIsNotNone(adapter_from_env({
            "TUMBLR_OWNER_BLOG": "myblog",
            "TUMBLR_OAUTH_ACCESS_TOKEN_FILE": str(self.token),
        }))
        os.chmod(self.token, 0o644)
        self.assertIsNone(adapter_from_env({
            "TUMBLR_OWNER_BLOG": "myblog",
            "TUMBLR_OAUTH_ACCESS_TOKEN_FILE": str(self.token),
        }))

    def test_invalid_blog_identifiers_rejected(self):
        for bad in ("https://example.com", "other.example.com", "../../etc", "abc/def"):
            with self.assertRaises(ValueError):
                normalize_blog(bad)
        self.assertEqual("myblog", normalize_blog("myblog.tumblr.com"))


if __name__ == "__main__":
    unittest.main()
