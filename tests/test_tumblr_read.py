"""Offline verification of the official Tumblr read-only posts bridge."""
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.tumblr_read import TumblrReadAdapter, adapter_from_env, normalize_blog, media_thumbnail


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
                 "post_url": "https://myblog.tumblr.com/post/123",
                 "content": [{"type": "video", "poster": [{"url": "https://64.media.tumblr.com/thumb.jpg"}]}]}
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
        self.assertEqual("video", result["view"]["rows"][0]["type"])
        self.assertEqual("https://64.media.tumblr.com/thumb.jpg", result["view"]["rows"][0]["thumbnail_url"])
        self.assertEqual(2, len(transport.calls))
        self.assertEqual("/v2/user/info", transport.calls[0])

    def test_all_owner_posts_are_reachable_with_explicit_pages(self):
        from urllib.parse import parse_qs,urlsplit
        class Pages(StubTransport):
            def get(self,path,bearer):
                if path=="/v2/user/info":
                    return super().get(path,bearer)
                self.calls.append(path)
                query=parse_qs(urlsplit(path).query)
                offset=int(query["offset"][0])
                posts=[{"summary":f"Post {i}","type":"text",
                        "date":"2026-11-11",
                        "post_url":f"https://myblog.tumblr.com/post/{i}"} for i in
                       range(offset,min(offset+20,46))]
                return {"meta":{"status":200},"response":{
                    "blog":{"name":"myblog"},"posts":posts,"total_posts":46}}
        transport=Pages()
        assistant=Assistant(bridge=Bridge({"tumblr":TumblrReadAdapter(
            "myblog",str(self.token),transport=transport)}),providers=[])
        first=assistant.turn(selected_action="tumblr.list_posts",
                             selected_target="myblog")
        self.assertEqual(first["state"],"completed")
        self.assertEqual(first["view"]["total"],46)
        self.assertTrue(first["view"]["total_verified"])
        self.assertEqual(len(first["view"]["rows"]),20)
        self.assertEqual(first["view"]["next_target"],"posts:20")
        second=assistant.turn(selected_action="tumblr.list_posts",
                              selected_target=first["view"]["next_target"])
        self.assertEqual(len(second["view"]["rows"]),20)
        self.assertEqual(second["view"]["prev_target"],"posts:0")
        third=assistant.turn(selected_action="tumblr.list_posts",
                             selected_target=second["view"]["next_target"])
        self.assertEqual(len(third["view"]["rows"]),6)
        self.assertEqual(third["view"]["total"],46)
        self.assertNotIn("next_target",third["view"])
        self.assertEqual(len({row["url"] for page in (first,second,third)
                              for row in page["view"]["rows"]}),46)
        self.assertEqual(len(transport.calls),6)
        self.assertTrue(all(path=="/v2/user/info" or
                            path.startswith("/v2/blog/myblog.tumblr.com/posts?")
                            for path in transport.calls))

    def test_owner_posts_not_misreported_as_complete_when_total_missing(self):
        class UnknownTotal(StubTransport):
            def get(self,path,bearer):
                if path=="/v2/user/info":
                    return super().get(path,bearer)
                self.calls.append(path)
                return {"meta":{"status":200},"response":{
                    "blog":{"name":"myblog"},"posts":[
                        {"summary":str(i),"post_url":f"https://myblog.tumblr.com/post/{i}"}
                        for i in range(20)]}}
        assistant=Assistant(bridge=Bridge({"tumblr":TumblrReadAdapter(
            "myblog",str(self.token),transport=UnknownTotal())}),providers=[])
        result=assistant.turn(selected_action="tumblr.list_posts",
                              selected_target="myblog")
        self.assertEqual(result["state"],"completed")
        self.assertIsNone(result["view"]["total"])
        self.assertFalse(result["view"]["total_verified"])
        self.assertEqual(result["view"]["next_target"],"posts:20")

    def test_invalid_owner_post_page_or_out_of_owner_scope_never_calls_api(self):
        transport=StubTransport()
        adapter=TumblrReadAdapter("myblog",str(self.token),transport=transport)
        for target in ("posts:-20","posts:21","posts:020","posts:abc",
                       "posts:1000000000000000","another"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    adapter.perform("tumblr.list_posts",target,{})
        self.assertEqual(transport.calls,[])

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

    def test_external_image_previews_are_rejected(self):
        self.assertEqual(("", ""), media_thumbnail({"content": [
            {"type": "image", "media": [{"url": "https://malicious.invalid/a.jpg"}]}
        ]}))

    def test_invalid_blog_identifiers_rejected(self):
        for bad in ("https://example.com", "other.example.com", "../../etc", "abc/def"):
            with self.assertRaises(ValueError):
                normalize_blog(bad)
        self.assertEqual("myblog", normalize_blog("myblog.tumblr.com"))


if __name__ == "__main__":
    unittest.main()
