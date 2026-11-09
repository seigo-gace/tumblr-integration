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
