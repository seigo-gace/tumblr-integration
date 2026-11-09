import unittest
from urllib.parse import urlparse, parse_qs
from tumblrkit.oauth import TumblrOAuth, TOKEN, USER_INFO

class FakeTransport:
    def __init__(self):
        self.posts = []
        self.reads = []
        self.token_result = {"access_token": "private-token", "token_type": "bearer", "refresh_token": "private-refresh", "expires_in": 3600}
    def post_form(self, url, data):
        self.posts.append((url, data))
        return self.token_result
    def get_json(self, url, access_token):
        self.reads.append((url, access_token))
        return {"response": {"user": {"name": "owned-blog"}}}

class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.oauth = TumblrOAuth("client", "secret", "https://example.org/oauth/callback", self.transport)
    def test_url_state_and_minimum_scope(self):
        start = self.oauth.begin()
        self.assertTrue(start.url.startswith("https://www.tumblr.com/oauth2/authorize?"))
        q = parse_qs(urlparse(start.url).query)
        self.assertEqual(q["state"], [start.state])
        self.assertEqual(q["scope"], ["basic"])
        self.assertEqual(q["redirect_uri"], ["https://example.org/oauth/callback"])
        self.assertNotIn("secret", start.url)
    def test_exchange_and_account_readback(self):
        started = self.oauth.begin()
        tokens = self.oauth.exchange("?state="+started.state+"&code=abc", started.state)
        self.assertEqual(self.oauth.verify_identity(tokens), "owned-blog")
        self.assertEqual(self.transport.posts[0][0], TOKEN)
        self.assertEqual(self.transport.reads[0][0], USER_INFO)
    def test_bad_state_never_requests_token(self):
        with self.assertRaises(PermissionError): self.oauth.exchange("state=wrong&code=abc", "expected")
        self.assertEqual(self.transport.posts, [])
    def test_missing_code_never_requests_token(self):
        with self.assertRaises(ValueError): self.oauth.exchange("state=expected", "expected")
        self.assertEqual(self.transport.posts, [])
    def test_denial_never_requests_token(self):
        with self.assertRaises(PermissionError): self.oauth.exchange("state=expected&error=access_denied", "expected")
        self.assertEqual(self.transport.posts, [])
    def test_duplicate_state_rejected(self):
        with self.assertRaises(ValueError): self.oauth.exchange("state=x&state=x&code=c", "x")
    def test_reject_invalid_token(self):
        self.transport.token_result = {"access_token": "", "token_type": "bearer"}
        with self.assertRaises(ValueError): self.oauth.exchange("state=x&code=c", "x")
    def test_bad_scope_rejected(self):
        with self.assertRaises(ValueError): self.oauth.begin(("admin",))
    def test_non_https_callback_rejected(self):
        with self.assertRaises(ValueError): TumblrOAuth("id", "secret", "http://example.org", self.transport)
    def test_missing_identity_rejected(self):
        self.transport.get_json = lambda url, token: {"response": {}}
        token = self.oauth.exchange("state=x&code=c", "x")
        with self.assertRaises(ValueError): self.oauth.verify_identity(token)

if __name__ == "__main__": unittest.main()
