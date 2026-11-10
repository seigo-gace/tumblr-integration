"""Offline end-to-end local drafts tests. No AI or Tumblr requests."""
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "drafts-test-token-abcdefghijklmnop"


def req(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response=conn.getresponse()
    status, payload=response.status, response.read()
    conn.close()
    return status, payload


class DraftEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release=build_release(ROOT/"app", Path(self.tmp.name)/"dist", "e"*40)
        self.service=AppServer(self.release,TOKEN,assistant=Assistant(providers=[]))
        self.service.__enter__()
        self.addCleanup(lambda:self.service.__exit__(None,None,None))
        self.auth={"Authorization":"Bearer "+TOKEN,"Content-Type":"application/json"}

    def test_save_list_delete_and_persist_without_ai(self):
        status, body=req(self.service.port,"GET","/drafts")
        self.assertEqual(status,401)
        status,body=req(self.service.port,"POST","/drafts",json.dumps({"text":"募集の下書き"}).encode(),self.auth)
        self.assertEqual(status,201)
        ident=json.loads(body)["id"]
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["drafts"][0]["text"],"募集の下書き")
        self.assertEqual(self.service.assistant.providers,[])
        status,body=req(self.service.port,"DELETE","/drafts/"+ident,headers=self.auth)
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)["deleted"])
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        self.assertEqual(json.loads(body)["drafts"],[])

    def test_restart_preserves_drafts_and_no_external_provider(self):
        status,body=req(self.service.port,"POST","/drafts",'{"text":"再起動しても保存"}'.encode("utf-8"),self.auth)
        self.assertEqual(status,201)
        with AppServer(self.release,TOKEN,assistant=Assistant(providers=[])) as other:
            status,body=req(other.port,"GET","/drafts",headers=self.auth)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["drafts"][0]["text"],"再起動しても保存")

    def test_strict_validation_and_origin(self):
        status,_=req(self.service.port,"POST","/drafts",b'{"text":""}',self.auth)
        self.assertEqual(status,400)
        status,_=req(self.service.port,"POST","/drafts",b'{"text":"x","side_effect":true}',self.auth)
        self.assertEqual(status,400)
        status,_=req(self.service.port,"POST","/drafts",b'{"text":"secret"}',{**self.auth,"Origin":"https://attacker.invalid"})
        self.assertEqual(status,403)
        status,_=req(self.service.port,"DELETE","/drafts/not-an-id",headers=self.auth)
        self.assertEqual(status,400)

    def test_saved_credentials_are_redacted(self):
        data=json.dumps({"text":"連絡 owner@example.com bearer abcdefghijklmnop"}).encode()
        status,body=req(self.service.port,"POST","/drafts",data,self.auth)
        self.assertEqual(status,201)
        self.assertNotIn(b"owner@example.com",body)
        self.assertNotIn(b"abcdefghijklmnop",body)


if __name__=="__main__":
    unittest.main()
