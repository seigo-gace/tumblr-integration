"""Real local HTTP/AppServer owner workflow; Tumblr calls use a strict fake."""
import http.client
import json
import os
import tempfile
import unittest
from pathlib import Path

from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.drafts import DraftStore
from tumblrkit.publisher_runtime import PublisherAdapter
from tumblrkit.tumblr_read import TumblrReadAdapter
from test_post_lifecycle import OfficialFake, Budget

ROOT=Path(__file__).resolve().parents[1]
OWNER="http-lifecycle-owner-token-abcdefghijkl"


def request(port,path,method="GET",body=None,authorized=True):
    conn=http.client.HTTPConnection("127.0.0.1",port,timeout=7)
    headers={"Content-Type":"application/json"}
    if authorized:
        headers["Authorization"]="Bearer "+OWNER
    data=json.dumps(body,ensure_ascii=False).encode("utf-8") if body is not None else None
    try:
        conn.request(method,path,body=data,headers=headers)
        res=conn.getresponse()
        raw=res.read(250000)
        return res.status,(json.loads(raw) if res.status in (200,201) else {})
    finally:
        conn.close()


class PostLifecycleOwnerHttpTests(unittest.TestCase):
    def test_ui_local_draft_edit_delete_flow_and_idempotent_owner_store(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            os.chmod(base,0o700)
            release=build_release(ROOT/"app",base/"dist","f"*40)
            drafts=DraftStore(base/"dist"/"data"/"drafts.sqlite3")
            fake=OfficialFake()
            budget=Budget()
            publisher=PublisherAdapter("owner",drafts,lambda:"test-owner-token",
                read_transport=fake,write_transport=fake,budget=budget)
            source=drafts.save("初回公開内容")
            self.assertTrue(publisher.perform(
                "tumblr.publish_draft",source["id"],{})["verified"])
            reader=TumblrReadAdapter("owner",base/"token",transport=fake,
                                     token_provider=lambda:"test-owner-token")
            reader.publisher=publisher
            assistant=Assistant(bridge=Bridge({"tumblr":reader}),providers=[])
            with AppServer(release,OWNER,assistant=assistant) as app:
                status,items=request(app.port,"/drafts")
                self.assertEqual(status,200)
                entry=next(d for d in items["drafts"] if d["id"]==source["id"])
                self.assertEqual(entry["publish_state"],"published")
                self.assertTrue(entry["locked"])
                self.assertEqual(entry["provider_post_id"],fake.post_id)
                status,revised=request(app.port,"/drafts","POST",
                                       {"text":"公開後の改訂全文"})
                self.assertEqual(status,201)
                target=source["id"]+":"+revised["id"]
                status,proposal=request(app.port,"/assistant","POST",{
                    "selected_action":"tumblr.edit_post","selected_target":target})
                self.assertEqual(status,200)
                self.assertEqual(proposal["state"],"confirmation_required")
                self.assertIn("初回公開内容",proposal["text"])
                self.assertIn("公開後の改訂全文",proposal["text"])
                status,result=request(app.port,"/assistant","POST",{
                    "confirm":True,"request_id":proposal["request_id"]})
                self.assertEqual(status,200)
                self.assertEqual(result["state"],"completed")
                self.assertEqual(fake.text,"公開後の改訂全文")
                status,items=request(app.port,"/drafts")
                entry=next(d for d in items["drafts"] if d["id"]==source["id"])
                self.assertEqual(entry["current_text"],"公開後の改訂全文")
                revision=next(d for d in items["drafts"] if d["id"]==revised["id"])
                self.assertTrue(revision["locked"])
                status,proposal=request(app.port,"/assistant","POST",{
                    "selected_action":"tumblr.delete_post",
                    "selected_target":source["id"]})
                self.assertEqual(proposal["state"],"confirmation_required")
                self.assertFalse(fake.deleted)
                status,result=request(app.port,"/assistant","POST",{
                    "confirm":True,"request_id":proposal["request_id"]})
                self.assertEqual(result["state"],"completed")
                self.assertTrue(fake.deleted)
                status,items=request(app.port,"/drafts")
                entry=next(d for d in items["drafts"] if d["id"]==source["id"])
                self.assertEqual(entry["publish_state"],"deleted")
                status,_=request(app.port,"/drafts",authorized=False)
                self.assertEqual(status,401)
                self.assertEqual(len([x for x in fake.calls if x[0]=="edit"]),1)
                self.assertEqual(len([x for x in fake.calls if x[0]=="delete"]),1)


if __name__=="__main__":
    unittest.main()
