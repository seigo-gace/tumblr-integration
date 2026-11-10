"""Offline Discovery acceptance: official tagged endpoint, bounded results, fail closed."""
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tumblrkit.bridge import Bridge, ActionRequest
from tumblrkit.tumblr_read import TumblrReadAdapter, OfficialTransport
from tumblrkit.api_safety import TumblrReadBudget

class TaggedStub:
    def __init__(self, payload=None):
        self.calls=[]
        self.payload=payload if payload is not None else {"meta":{"status":200},"response":[
          {"blog_name":"one", "id_string":"123","post_url":"https://one.tumblr.com/post/123",
           "summary":"example", "type":"text","date":"2026-11-11"},
          {"blog_name":"one","id_string":"123","post_url":"https://one.tumblr.com/post/123","summary":"duplicate"},
          {"blog_name":"one","id_string":"124","post_url":"https://evil.invalid/post/124","summary":"offsite"}]}
    def get(self,path,token):
        self.calls.append(path)
        return self.payload

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        os.chmod(self.root,0o700)
        self.token=self.root/"access-token"
        self.token.write_text("nonreal-test-access-token")
        os.chmod(self.token,0o600)

    def test_tag_query_encoding_dedup_verified_result(self):
        transport=TaggedStub()
        adapter=TumblrReadAdapter("myblog",self.token,transport=transport)
        result=Bridge({"tumblr":adapter}).dispatch(ActionRequest(
            "discover-1","tumblr.discover","日本語 タグ"))
        self.assertEqual(result.state,"completed")
        self.assertTrue(result.detail["verified"])
        self.assertEqual(len(result.detail["rows"]),1)
        self.assertEqual(len(transport.calls),1)
        self.assertEqual(urlsplit(transport.calls[0]).path,"/v2/tagged")
        query=parse_qs(urlsplit(transport.calls[0]).query)
        self.assertEqual(query["tag"],["日本語 タグ"])
        self.assertEqual(query["limit"],["10"])

    def test_invalid_tag_does_not_call_network(self):
        stub=TaggedStub()
        adapter=TumblrReadAdapter("myblog",self.token,transport=stub)
        for tag in ("../secret","x"*65,"test\nnext","hello?hack=1"):
            result=Bridge({"tumblr":adapter}).dispatch(ActionRequest(
                "bad-"+str(len(tag))+"-"+tag[:3],"tumblr.discover",tag))
            self.assertEqual(result.state,"failed")
        with self.assertRaises(ValueError):
            Bridge({"tumblr":adapter}).dispatch(ActionRequest("bad-empty","tumblr.discover",""))
        self.assertEqual(stub.calls,[])

    def test_non_verified_response_fails_closed(self):
        adapter=TumblrReadAdapter("myblog",self.token,transport=TaggedStub({"meta":{"status":401},"response":[]}))
        result=Bridge({"tumblr":adapter}).dispatch(ActionRequest("bad-resp","tumblr.discover","安全"))
        self.assertEqual(result.state,"failed")

    def test_official_transport_reserves_one_call_before_network(self):
        clock=[1800000000]
        budget=TumblrReadBudget(self.root/"budget.sqlite",clock=lambda:clock[0])
        adapter=TumblrReadAdapter("myblog",self.token)
        adapter.transport.budget=budget
        budget.reserve(1)
        with patch("tumblrkit.tumblr_read.urlopen") as net:
            with self.assertRaisesRegex(ValueError,"tumblr_budget_cooldown"):
                adapter.perform("tumblr.discover","tag",{})
            net.assert_not_called()

if __name__=="__main__":
    unittest.main()
