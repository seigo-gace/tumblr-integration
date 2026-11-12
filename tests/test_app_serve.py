import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tumblrkit.appserve import AppServer, ServeError, build_release, rollback_release

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SHA_A = "a" * 40
SHA_B = "b" * 40
TOKEN = "test-access-token-1"


def fetch(url, token=None, method="GET", data=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer " + token
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers)


class AppServeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.tmp = Path(self.work.name)

    def test_build_stamps_commit_and_keeps_script_bytes(self):
        release = build_release(APP, self.tmp, SHA_A)
        html = (release / "index.html").read_text(encoding="utf-8")
        self.assertIn(SHA_A, html)
        self.assertNotIn("__TUMBLR_COMMIT__", html)
        self.assertEqual((release / "app.js").read_bytes(), (APP / "app.js").read_bytes())
        self.assertIn('id="oauth-status"', html)
        client=(release / "app.js").read_text(encoding="utf-8")
        self.assertIn('fetch("/oauth/status"', client)
        self.assertIn("oauthStatus.textContent=", client)
        self.assertIn('post({message:text})', client)
        self.assertIn('view.related_basis==="observed_cooccurring_tags"', client)
        self.assertIn("function showMoreRelated()", client)
        self.assertIn("until=Math.min(shown+8,view.related_tags.length)", client)
        self.assertIn("関連タグをさらに表示", client)
        for action in ("engagement.like","engagement.reblog","engagement.follow"):
            self.assertIn('selected_action:spec[0]',client)
            self.assertIn(action,client)
        meta = json.loads((release / "RELEASE.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["files"]["app.js"], hashlib.sha256((APP / "app.js").read_bytes()).hexdigest())

    def test_screen_requires_token_and_health_has_no_screen(self):
        build_release(APP, self.tmp, SHA_A)
        with AppServer(self.tmp / "current", TOKEN) as server:
            base = f"http://127.0.0.1:{server.port}"
            status, body, _ = fetch(base + "/healthz")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["commit"], SHA_A)
            self.assertNotIn(b"<form", body)
            status, body, _ = fetch(base + "/")
            self.assertEqual(status, 401)
            self.assertNotIn(b"id=\"chat-form\"", body)
            status, body, _ = fetch(base + "/", TOKEN)
            self.assertEqual(status, 200)
            self.assertIn(b"id=\"chat-form\"", body)
            self.assertIn(SHA_A.encode(), body)
            status, _, _ = fetch(base + "/app.js", "wrong-access-token")
            self.assertEqual(status, 401)
            status, _, _ = fetch(base + "/styles.css?token=" + TOKEN)
            self.assertEqual(status, 400)

    def test_session_cookie_opens_the_screen_once(self):
        build_release(APP, self.tmp, SHA_A)
        with AppServer(self.tmp / "current", TOKEN) as server:
            base = f"http://127.0.0.1:{server.port}"
            status, _, headers = fetch(
                base + "/session",
                method="POST",
                data=b"token=" + TOKEN.encode(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 204)
            cookie = headers["Set-Cookie"].split(";", 1)[0]
            status, body, _ = fetch(base + "/app.js", headers={"Cookie": cookie})
            self.assertEqual(status, 200)
            self.assertIn(b'fetch("/assistant"', body)
            self.assertNotIn(b"https://", body)
            self.assertNotIn(b"localStorage", body)

    def test_oauth_routes_require_owner_and_state_and_never_return_tokens(self):
        import os
        import http.client
        from urllib.parse import parse_qs, urlsplit
        from tumblrkit.account_runtime import OwnerOAuth
        from test_account_runtime import FakeTransport
        build_release(APP, self.tmp, SHA_A)
        secret_dir = self.tmp / "private-oauth"
        secret_dir.mkdir(mode=0o700)
        os.chmod(secret_dir, 0o700)
        for name, value in (("consumer-key", "test-consumer-key-123"), ("client-secret", "test-consumer-secret-123")):
            path = secret_dir / name
            path.write_text(value)
            os.chmod(path, 0o600)
        account = OwnerOAuth(secret_dir, "recruitment", FakeTransport())
        with AppServer(self.tmp / "current", TOKEN) as server:
            server.httpd.owner_oauth = account
            base = "http://127.0.0.1:" + str(server.port)
            status, _, _ = fetch(base + "/oauth/status")
            self.assertEqual(status, 401)
            status, _, _ = fetch(base + "/oauth/start", method="POST", data=b"")
            self.assertEqual(status, 401)
            status, body, _ = fetch(base + "/oauth/start", TOKEN, method="POST",
                                    data=b"", headers={"Origin": base})
            self.assertEqual(status, 200)
            url = json.loads(body)["authorize_url"]
            query = parse_qs(urlsplit(url).query)
            state = query["state"][0]
            status, body, _ = fetch(base + "/oauth/callback?state=wrong&code=valid")
            self.assertEqual(status, 400)
            status, body, _ = fetch(base + "/oauth/callback?state=" + state + "&code=valid")
            self.assertEqual(status, 200)
            self.assertNotIn(b"initial-access-token", body)
            status, body, _ = fetch(base + "/oauth/status", TOKEN)
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["connected"])
            # After a real OAuth callback both publishing and Engagement
            # adapters must be rebound to this *same* verified owner; a
            # disconnected class name is not implementation.
            self.assertIn("tumblr",server.httpd.assistant.bridge.adapters)
            self.assertIn("engagement",server.httpd.assistant.bridge.adapters)
            owner=server.httpd.assistant.bridge.adapters["tumblr"]
            engage=server.httpd.assistant.bridge.adapters["engagement"]
            self.assertIs(owner.engagement,engage)
            self.assertEqual(engage.owner,"recruitment")
            self.assertEqual(engage.store.path,
                             secret_dir / "discovery.sqlite3")
            status, _, _ = fetch(base + "/oauth/callback?state=" + state + "&code=valid")
            self.assertEqual(status, 400)

    def test_discovery_cache_and_exclusions_are_owner_only_and_offline(self):
        import os
        from tumblrkit.discovery_store import DiscoveryStore
        build_release(APP, self.tmp, SHA_A)
        private = self.tmp / "discovery-private"
        private.mkdir(mode=0o700)
        os.chmod(private,0o700)
        with AppServer(self.tmp / "current", TOKEN) as server:
            server.httpd.discovery_store = DiscoveryStore(private / "state.sqlite3")
            base = f"http://127.0.0.1:{server.port}"
            status,_,_ = fetch(base + "/discovery/candidates")
            self.assertEqual(status,401)
            status,body,_ = fetch(base + "/discovery/candidates",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["posts"],[])
            exclusion=json.dumps({"blog":"example"}).encode()
            status,_,_ = fetch(base + "/discovery/exclude",method="POST",data=exclusion,
                               headers={"Content-Type":"application/json","Origin":base})
            self.assertEqual(status,401)
            status,_,_ = fetch(base + "/discovery/exclude",TOKEN,method="POST",data=exclusion,
                               headers={"Content-Type":"application/json","Origin":"https://evil.invalid"})
            self.assertEqual(status,403)
            status,body,_ = fetch(base + "/discovery/exclude",TOKEN,method="POST",data=exclusion,
                                  headers={"Content-Type":"application/json","Origin":base})
            self.assertEqual(status,200)
            self.assertTrue(json.loads(body)["excluded"])
            self.assertEqual(server.httpd.discovery_store.excluded_blogs(),{"example"})

    def test_discovery_one_post_recheck_is_owner_only_and_offline(self):
        import os
        from tumblrkit.discovery_store import DiscoveryStore
        from tumblrkit.tumblr_read import TumblrReadAdapter
        from tumblrkit.bridge import Bridge
        from tumblrkit.assistant import Assistant
        class Stub:
            def __init__(self):
                self.calls=[]
                self.results=[
                    {"meta":{"status":200},"response":{"posts":[{"id_string":"123","blog_name":"one"}]}},
                    ValueError("tumblr_post_unavailable"),
                ]
            def get(self,path,token):
                self.calls.append(path)
                output=self.results.pop(0)
                if isinstance(output,BaseException): raise output
                return output
        private=self.tmp/"private-discovery"
        private.mkdir(mode=0o700)
        os.chmod(private,0o700)
        tokenfile=private/"access-token"
        tokenfile.write_text("nonreal-dummy-oauth-token")
        os.chmod(tokenfile,0o600)
        store=DiscoveryStore(private/"discovery.sqlite3")
        store.save_page("tag:test",[{"name":"one","url":"https://one.tumblr.com"}],[
            {"candidate_id":"one:123","blog":"one","post_id":"123",
             "url":"https://one.tumblr.com/post/123","title":"test","type":"text",
             "date":"","thumbnail_url":"","text_excerpt":"","tags":[],
             "note_count":0,"media_refs":[]},
        ],1,complete=True)
        stub=Stub()
        assistant=Assistant(bridge=Bridge({"tumblr":TumblrReadAdapter("owner",tokenfile,transport=stub)}),providers=[])
        build_release(APP,self.tmp,SHA_A)
        with AppServer(self.tmp/"current",TOKEN,assistant=assistant) as server:
            base=f"http://127.0.0.1:{server.port}"
            uri=base+"/discovery/recheck"
            payload=json.dumps({"candidate_id":"one:123"}).encode()
            headers={"Content-Type":"application/json","Origin":base}
            status,_,_=fetch(uri,method="POST",data=payload,headers=headers)
            self.assertEqual(status,401)
            status,_,_=fetch(uri,TOKEN,method="POST",data=payload,
                             headers={"Content-Type":"application/json","Origin":"https://attacker.invalid"})
            self.assertEqual(status,403)
            self.assertEqual(stub.calls,[])
            status,body,_=fetch(uri,TOKEN,method="POST",data=payload,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["state"],"visible")
            self.assertEqual(len(stub.calls),1)
            status,body,_=fetch(uri,TOKEN,method="POST",data=payload,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["state"],"unavailable")
            self.assertEqual(len(stub.calls),2)
            self.assertEqual(store.read_candidates(),[])

    def test_library_http_owner_only_bulk_update_and_resume(self):
        import os
        from tumblrkit.discovery_store import DiscoveryStore
        from tumblrkit.library import Library
        private=self.tmp/"library-private"
        private.mkdir(mode=0o700)
        os.chmod(private,0o700)
        store=DiscoveryStore(private/"discovery.sqlite3")
        store.save_page("tag:test",[{"name":"alpha","url":"https://alpha.tumblr.com"}],[{
            "candidate_id":"alpha:123","blog":"alpha","post_id":"123",
            "url":"https://alpha.tumblr.com/post/123","title":"test image",
            "type":"image","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,"media_refs":[{"url":"https://64.media.tumblr.com/photo.jpg","type":"image"}],
        }],1,complete=True)
        build_release(APP,self.tmp,SHA_A)
        with AppServer(self.tmp/"current",TOKEN) as server:
            server.httpd.library=Library(store)
            base=f"http://127.0.0.1:{server.port}"
            status,_,_=fetch(base+"/library")
            self.assertEqual(status,401)
            status,body,_=fetch(base+"/library",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["total"],1)
            self.assertEqual(len(json.loads(body)["items"][0]["media"]),1)
            selected=json.dumps({"candidate_ids":["alpha:123"]}).encode()
            headers={"Content-Type":"application/json","Origin":base}
            status,_,_=fetch(base+"/library/selected",method="POST",
                             data=selected,headers=headers)
            self.assertEqual(status,401)
            status,_,_=fetch(base+"/library/selected",TOKEN,method="POST",
                             data=selected,headers={"Content-Type":"application/json",
                                                    "Origin":"https://attacker.invalid"})
            self.assertEqual(status,403)
            status,body,_=fetch(base+"/library/selected",TOKEN,method="POST",
                                data=selected,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["items"][0]["candidate_id"],"alpha:123")
            self.assertEqual(len(json.loads(body)["items"][0]["media"]),1)
            status,_,_=fetch(base+"/library/selected",TOKEN,method="POST",
                             data=json.dumps({"candidate_ids":["alpha:999"]}).encode(),
                             headers=headers)
            self.assertEqual(status,400)
            update=json.dumps({"candidate_ids":["alpha:123"],"field":"favorite","value":True}).encode()
            headers={"Content-Type":"application/json","Origin":base}
            status,_,_=fetch(base+"/library/update",method="POST",data=update,headers=headers)
            self.assertEqual(status,401)
            status,_,_=fetch(base+"/library/update",TOKEN,method="POST",data=update,
                headers={"Content-Type":"application/json","Origin":"https://evil.invalid"})
            self.assertEqual(status,403)
            status,body,_=fetch(base+"/library/update",TOKEN,method="POST",data=update,headers=headers)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["updated"],1)
            status,body,_=fetch(base+"/library?filter=favorite",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["total"],1)
            progress=json.dumps({"candidate_id":"alpha:123","media_index":0}).encode()
            status,body,_=fetch(base+"/library/progress",TOKEN,method="POST",data=progress,headers=headers)
            self.assertEqual(status,200)
            self.assertTrue(json.loads(body)["verified"])
            status,body,_=fetch(base+"/library?filter=seen",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["last_position"]["candidate_id"],"alpha:123")
            status,_,_=fetch(base+"/library?filter=bad",TOKEN)
            self.assertEqual(status,400)
            status,_,_=fetch(base+"/library/update",TOKEN,method="POST",
                              data=json.dumps({"candidate_ids":["alpha:999"],"field":"seen","value":True}).encode(),
                              headers=headers)
            self.assertEqual(status,400)
        self.assertEqual(Library(store).list("favorite")["total"],1)

    def test_media_save_private_http_end_to_end_without_tumblr(self):
        import os
        from tumblrkit.discovery_store import DiscoveryStore
        from tumblrkit.media_save import MediaSave
        class Downloader:
            def __init__(self):self.calls=[]
            def probe(self,url):
                self.calls.append("HEAD")
                return 64
            def fetch(self,url,path,expected):
                self.calls.append("GET")
                with open(path,"xb") as f:f.write(b"\x00"*64)
                return 64
        class Transcoder:
            def convert(self,src,dst):
                dst.write_bytes(b"\x00\x00\x00\x18ftypisom"+b"x"*64)
                return dst.stat().st_size
        folder=self.tmp/"media-private"
        folder.mkdir(mode=0o700)
        os.chmod(folder,0o700)
        store=DiscoveryStore(folder/"discovery.sqlite3")
        store.save_page("tag:test",[{"name":"owner","url":"https://owner.tumblr.com"}],[{
            "candidate_id":"owner:55","blog":"owner","post_id":"55",
            "url":"https://owner.tumblr.com/post/55","title":"clip",
            "type":"video","date":"","thumbnail_url":"","text_excerpt":"",
            "tags":[],"note_count":0,
            "media_refs":[{"url":"https://64.media.tumblr.com/clip.mp4","type":"video"}]
        }],1,complete=True)
        media_root=folder/"media"
        media_root.mkdir(mode=0o700)
        stub=Downloader()
        build_release(APP,self.tmp,SHA_A)
        with AppServer(self.tmp/"current",TOKEN) as server:
            server.httpd.media_save=MediaSave(store,media_root,stub,Transcoder())
            base=f"http://127.0.0.1:{server.port}"
            status,_,_=fetch(base+"/media/jobs")
            self.assertEqual(status,401)
            status,body,_=fetch(base+"/media/jobs",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["jobs"],[])
            payload=json.dumps({"candidate_id":"owner:55","media_index":0,"rights_confirmed":True}).encode()
            no_rights=json.dumps({"candidate_id":"owner:55","media_index":0,"rights_confirmed":False}).encode()
            headers={"Content-Type":"application/json","Origin":base}
            status,_,_=fetch(base+"/media/save",method="POST",data=payload,headers=headers)
            self.assertEqual(status,401)
            status,_,_=fetch(base+"/media/save",TOKEN,method="POST",data=payload,
                             headers={"Content-Type":"application/json","Origin":"https://evil.invalid"})
            self.assertEqual(status,403)
            status,_,_=fetch(base+"/media/save",TOKEN,method="POST",data=no_rights,headers=headers)
            self.assertEqual(status,400)
            self.assertEqual(stub.calls,[])
            status,body,_=fetch(base+"/media/save",TOKEN,method="POST",data=payload,headers=headers)
            self.assertEqual(status,200)
            result=json.loads(body)
            self.assertTrue(result["verified"])
            self.assertEqual(result["state"],"completed")
            self.assertEqual(stub.calls,["HEAD","GET"])
            job_id=result["job_id"]
            status,_,_=fetch(base+"/media/file/"+job_id)
            self.assertEqual(status,401)
            status,body,_=fetch(base+"/media/file/"+job_id,TOKEN)
            self.assertEqual(status,200)
            self.assertIn(b"ftyp",body)
            status,body,_=fetch(base+"/media/save",TOKEN,method="POST",data=payload,headers=headers)
            self.assertEqual(status,200)
            self.assertTrue(json.loads(body)["from_cache"])
            self.assertEqual(stub.calls,["HEAD","GET"])
            status,body,_=fetch(base+"/media/jobs",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["jobs"][0]["state"],"completed")
            self.assertEqual(json.loads(body)["total"],1)
            self.assertFalse(json.loads(body)["has_next"])
            status,body,_=fetch(base+"/media/jobs?offset=0&limit=1",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["total"],1)
            status,body,_=fetch(base+"/media/jobs?offset=1&limit=1",TOKEN)
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["jobs"],[])
            status,_,_=fetch(base+"/media/jobs?offset=-1",TOKEN)
            self.assertEqual(status,400)
            status,_,_=fetch(base+"/media/jobs?limit=1000",TOKEN)
            self.assertEqual(status,400)
            status,_,_=fetch(base+"/media/jobs?other=1",TOKEN)
            self.assertEqual(status,400)
        self.assertEqual(MediaSave(store,media_root,stub,Transcoder()).list()["jobs"][0]["source_post_id"],"55")

    def test_rollback_restores_previous_commit(self):
        build_release(APP, self.tmp, SHA_A)
        build_release(APP, self.tmp, SHA_B)
        self.assertEqual(rollback_release(self.tmp), SHA_A)
        self.assertEqual((self.tmp / "current-commit.txt").read_text(encoding="utf-8").strip(), SHA_A)
        self.assertEqual(rollback_release(self.tmp), SHA_B)

    def test_public_bind_bad_commit_and_tamper_are_refused(self):
        with self.assertRaises(ServeError):
            AppServer(APP, TOKEN, host="0.0.0.0")
        with self.assertRaises(ServeError):
            build_release(APP, self.tmp, "not-a-commit")
        release = build_release(APP, self.tmp, SHA_A)
        meta_path = release / "RELEASE.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["files"]["app.js"] = "0" * 64
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        with self.assertRaises(ServeError):
            with AppServer(release, TOKEN):
                pass
        with self.assertRaises(ServeError):
            rollback_release(self.tmp)


if __name__ == "__main__":
    unittest.main()
