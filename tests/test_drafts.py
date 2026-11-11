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

    def test_edit_existing_draft_preserves_id_and_rejects_stale_edit(self):
        status,body=req(self.service.port,"POST","/drafts",
                        json.dumps({"text":"初稿"}).encode(),self.auth)
        self.assertEqual(status,201)
        draft_id=json.loads(body)["id"]
        payload={"text":"修正版","expected_text":"初稿"}
        status,body=req(self.service.port,"PUT","/drafts/"+draft_id,
                        json.dumps(payload).encode(),self.auth)
        self.assertEqual(status,200)
        self.assertEqual(json.loads(body)["id"],draft_id)
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        drafts=json.loads(body)["drafts"]
        self.assertEqual(len(drafts),1)
        self.assertEqual(drafts[0]["id"],draft_id)
        self.assertEqual(drafts[0]["text"],"修正版")
        self.assertFalse(drafts[0]["locked"])
        status,body=req(self.service.port,"PUT","/drafts/"+draft_id,
                        json.dumps({"text":"古い更新","expected_text":"初稿"}).encode(),
                        self.auth)
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"draft_edit_conflict")
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        self.assertEqual(json.loads(body)["drafts"][0]["text"],"修正版")
        with AppServer(self.release,TOKEN,assistant=Assistant(providers=[])) as restarted:
            status,body=req(restarted.port,"GET","/drafts",headers=self.auth)
            self.assertEqual(json.loads(body)["drafts"][0]["id"],draft_id)
            self.assertEqual(json.loads(body)["drafts"][0]["text"],"修正版")

    def test_publish_reservation_locks_draft_update_and_delete(self):
        import sqlite3
        status,body=req(self.service.port,"POST","/drafts",
                        json.dumps({"text":"この本文は送信予約済"}).encode(),self.auth)
        self.assertEqual(status,201)
        ident=json.loads(body)["id"]
        from tumblrkit.publisher_runtime import PUBLISH_SCHEMA
        with sqlite3.connect(self.service.httpd.draft_store.path) as db:
            db.executescript(PUBLISH_SCHEMA)
            db.execute("INSERT INTO tumblr_publish_receipts "
                       "(draft_id,digest,state,updated_at) "
                       "VALUES(?,?,?,?)",(ident,"digest","reserved",1))
        status,body=req(self.service.port,"PUT","/drafts/"+ident,
                        json.dumps({"text":"変えてはいけない",
                                    "expected_text":"この本文は送信予約済"}).encode(),
                        self.auth)
        self.assertEqual(status,409)
        self.assertEqual(json.loads(body)["error"],"draft_publish_locked")
        status,body=req(self.service.port,"DELETE","/drafts/"+ident,headers=self.auth)
        self.assertEqual(status,409)
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        drafts=json.loads(body)["drafts"]
        self.assertEqual(drafts[0]["text"],"この本文は送信予約済")
        self.assertTrue(drafts[0]["locked"])

    def test_edit_endpoint_auth_origin_and_input_guards(self):
        status,body=req(self.service.port,"POST","/drafts",
                        json.dumps({"text":"原文"}).encode(),self.auth)
        ident=json.loads(body)["id"]
        url="/drafts/"+ident
        payload=json.dumps({"text":"本文","expected_text":"原文"}).encode()
        status,_=req(self.service.port,"PUT",url,payload,{"Content-Type":"application/json"})
        self.assertEqual(status,401)
        status,_=req(self.service.port,"PUT",url,payload,
                     {**self.auth,"Origin":"https://attacker.invalid"})
        self.assertEqual(status,403)
        status,_=req(self.service.port,"PUT",url,
                     json.dumps({"text":"本文","expected_text":"原文",
                                 "extra":"not_allowed"}).encode(),self.auth)
        self.assertEqual(status,400)
        status,_=req(self.service.port,"PUT",url,
                     json.dumps({"text":"本文"}).encode(),self.auth)
        self.assertEqual(status,400)
        status,_=req(self.service.port,"PUT","/drafts/bad",payload,self.auth)
        self.assertEqual(status,400)
        status,body=req(self.service.port,"GET","/drafts",headers=self.auth)
        self.assertEqual(json.loads(body)["drafts"][0]["text"],"原文")

    def test_saved_credentials_are_redacted(self):
        data=json.dumps({"text":"連絡 owner@example.com bearer abcdefghijklmnop"}).encode()
        status,body=req(self.service.port,"POST","/drafts",data,self.auth)
        self.assertEqual(status,201)
        self.assertNotIn(b"owner@example.com",body)
        self.assertNotIn(b"abcdefghijklmnop",body)


if __name__=="__main__":
    unittest.main()
