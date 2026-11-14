"""Browser login and session tests: localhost only, no external APIs."""
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from tumblrkit.appserve import AppServer, build_release
from tumblrkit.assistant import Assistant

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "local-test-token-abcdefghijklmnop"


def request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    status, payload, response_headers = response.status, response.read(), dict(response.getheaders())
    conn.close()
    return status, payload, response_headers


class LoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.release = build_release(ROOT / "app", Path(temp.name), "d" * 40)
        self.server = AppServer(self.release, TOKEN, assistant=Assistant(providers=[]))
        self.server.__enter__()
        self.addCleanup(lambda: self.server.__exit__(None, None, None))

    def test_public_root_is_login_but_never_returns_operation_app(self):
        status, body, _ = request(self.server.port, "GET", "/")
        self.assertEqual(status, 401)
        self.assertIn(b'action="/session"', body)
        self.assertNotIn(b'id="chat-form"', body)
        status, body, _ = request(self.server.port, "GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn(b'type="password"', body)

    def test_query_parameters_do_not_hide_login_layout(self):
        for path in ("/?source=link", "/index.html?utm_source=browser", "/login?ref=link"):
            status, body, _ = request(self.server.port, "GET", path)
            self.assertIn(status, (200, 401))
            self.assertIn(b'action="/session"', body)
            self.assertNotIn(b"bad request", body)
        status, _, _ = request(self.server.port, "GET", "/healthz?unexpected=1")
        self.assertEqual(status, 400)

    def test_login_csrf_allows_proxy_rewritten_origin_without_trusting_fake_forms(self):
        from html.parser import HTMLParser

        class HiddenField(HTMLParser):
            def __init__(self):
                super().__init__()
                self.nonce = None

            def handle_starttag(self, tag, attrs):
                fields = dict(attrs)
                if tag == "input" and fields.get("name") == "csrf":
                    self.nonce = fields.get("value")

        status, page, _ = request(self.server.port, "GET", "/login")
        self.assertEqual(status, 200)
        parser = HiddenField()
        parser.feed(page.decode("utf-8"))
        self.assertTrue(parser.nonce)
        from urllib.parse import urlencode
        body = urlencode({"token": TOKEN, "csrf": parser.nonce}).encode()
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
            "Origin": "https://proxy-rewrites.example.invalid",
            "Sec-Fetch-Site": "cross-site",
        }
        status, _, response = request(self.server.port, "POST", "/session", body, headers)
        self.assertEqual(status, 303)
        self.assertIn("Set-Cookie", response)
        status, _, _ = request(self.server.port, "POST", "/session", body, headers)
        self.assertEqual(status, 403)

    def test_wrong_password_shows_form_not_plain_unauthorized(self):
        status, content, _ = request(self.server.port, "POST", "/session",
            b"token=wrong", {"Content-Type":"application/x-www-form-urlencoded",
                            "Accept":"text/html"})
        self.assertEqual(status, 401)
        self.assertIn(b'action="/session"', content)
        self.assertIn('ログインキーが一致しません'.encode(), content)

    def test_login_creates_distinct_opaque_session_not_master_token(self):
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"}
        body = b"token=" + TOKEN.encode()
        status, _, login = request(self.server.port, "POST", "/session", body, headers)
        self.assertEqual(status, 303)
        self.assertEqual(login["Location"], "/")
        cookie = login["Set-Cookie"]
        self.assertNotIn(TOKEN, cookie)
        for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age="):
            self.assertIn(attribute, cookie)
        session_cookie = cookie.split(";", 1)[0]
        status, data, _ = request(self.server.port, "GET", "/", headers={"Cookie": session_cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'id="chat-form"', data)
        status, _, _ = request(self.server.port, "GET", "/app.js")
        self.assertEqual(status, 401)

    def test_cloudflare_tunnel_internal_host_accepts_exact_public_origin(self):
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "text/html",
            "Host": "127.0.0.1:8081",
            "Origin": "https://tumblr.hentaigay.org",
        }
        status, _, response_headers = request(
            self.server.port, "POST", "/session",
            b"token=" + TOKEN.encode(), headers,
        )
        self.assertEqual(status, 303)
        self.assertIn("Set-Cookie", response_headers)

    def test_tunnel_origin_does_not_trust_wrong_host_or_similar_domain(self):
        base = {"Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html"}
        for headers in (
            {"Origin": "https://tumblr.hentaigay.org.evil.invalid", "Host": "127.0.0.1:8081"},
            {"Origin": "http://tumblr.hentaigay.org", "Host": "127.0.0.1:8081"},
        ):
            status, _, _ = request(
                self.server.port, "POST", "/session",
                b"token=" + TOKEN.encode(), {**base, **headers},
            )
            self.assertEqual(status, 403)

    def test_public_origin_remains_valid_with_tunnel_rewritten_host(self):
        for host in ("127.0.0.1:8081", "localhost:8081", "tumblr.hentaigay.org",
                     "custom-tunnel-origin.internal"):
            status, _, headers = request(
                self.server.port, "POST", "/session",
                b"token=" + TOKEN.encode(),
                {"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "text/html",
                 "Host": host,
                 "Origin": "https://tumblr.hentaigay.org"},
            )
            self.assertEqual(status, 303)
            self.assertIn("Set-Cookie", headers)

    def test_unicode_owner_key_session_login(self):
        from tumblrkit.appserve import AppServer
        secret = "日本語パスワード!"
        self.assertGreaterEqual(len(secret), 8)
        with tempfile.TemporaryDirectory() as work:
            release = build_release(ROOT / "app", Path(work), "f" * 40)
            with AppServer(release, secret) as service:
                from urllib.parse import urlencode
                data = urlencode({"token": secret}).encode()
                status, _, headers = request(service.port, "POST", "/session", data,
                    {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"})
                self.assertEqual(status, 303)
                self.assertIn("Set-Cookie", headers)

    def test_wrong_login_and_cross_site_login_are_refused(self):
        base = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"}
        status, _, _ = request(self.server.port, "POST", "/session", b"token=wrong", base)
        self.assertEqual(status, 401)
        status, _, _ = request(self.server.port, "POST", "/session", b"token=" + TOKEN.encode(),
            {**base, "Origin": "https://attacker.invalid"})
        self.assertEqual(status, 403)
        status, _, _ = request(self.server.port, "GET", "/")
        self.assertEqual(status, 401)

    def test_session_can_post_only_to_same_origin(self):
        status, _, headers = request(self.server.port, "POST", "/session", b"token=" + TOKEN.encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 204)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        body = json.dumps({"message": "投稿案の相談"}).encode()
        host = f"127.0.0.1:{self.server.port}"
        base = {"Cookie": cookie, "Content-Type": "application/json"}
        status, _, _ = request(self.server.port, "POST", "/assistant", body, base)
        self.assertEqual(status, 403)
        status, payload, _ = request(self.server.port, "POST", "/assistant", body, {**base, "Origin": f"http://{host}"})
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(payload)["executed"])
        status, _, _ = request(self.server.port, "POST", "/assistant", body,
            {**base, "Origin": "https://attacker.invalid"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
