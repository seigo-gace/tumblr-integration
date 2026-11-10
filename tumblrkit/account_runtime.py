"""Owner-only Tumblr OAuth2 account lifecycle for the existing operation app.

No credentials are supplied by web clients. OAuth state is single-use and short lived;
tokens are stored outside Git, in an owner-private directory.
"""
import json
import os
import secrets
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from tumblrkit.oauth import TumblrOAuth, AccountTokens, TOKEN, USER_INFO, read_owner_client_credentials
from tumblrkit.tumblr_read import normalize_blog

CALLBACK = "https://tumblr.hentaigay.org/oauth/callback"
STATE_TTL = 600
MAX_RESPONSE = 65536


class OAuthNetwork:
    def post_form(self, url, data):
        if url != TOKEN:
            raise ValueError("oauth_destination_forbidden")
        body = urlencode(data).encode("utf-8")
        request = Request(url, data=body, method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded",
                                   "Accept": "application/json",
                                   "User-Agent": "TGtumbler-Operation-Assistant/1.0"})
        return self._json(request)

    def get_json(self, url, access_token):
        if url != USER_INFO:
            raise ValueError("oauth_destination_forbidden")
        return self._json(Request(url, method="GET", headers={
            "Authorization": "Bearer " + access_token,
            "Accept": "application/json",
            "User-Agent": "TGtumbler-Operation-Assistant/1.0"}))

    @staticmethod
    def _json(request):
        try:
            with urlopen(request, timeout=12) as response:
                if response.status != 200:
                    raise ValueError("oauth_http_status")
                raw = response.read(MAX_RESPONSE + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            raise ValueError("oauth_network_unavailable") from None
        if len(raw) > MAX_RESPONSE:
            raise ValueError("oauth_response_too_large")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError("oauth_response_invalid") from None
        if not isinstance(result, dict):
            raise ValueError("oauth_response_invalid")
        return result


class PrivateTokenStore:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser()
        self.tokens = self.folder / "account-tokens.json"
        self.access = self.folder / "access-token"

    def _private_dir(self):
        if self.folder.is_symlink() or not self.folder.is_dir():
            raise ValueError("oauth_directory_unavailable")
        info = self.folder.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("oauth_directory_unsafe")

    def _safe_file(self, path):
        if path.is_symlink():
            raise ValueError("oauth_file_symlink")
        if not path.exists():
            return False
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("oauth_file_unsafe")
        return True

    def read(self):
        self._private_dir()
        if not self._safe_file(self.tokens):
            return None
        if self.tokens.stat().st_size > 16384:
            raise ValueError("oauth_file_too_large")
        payload = json.loads(self.tokens.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not all(isinstance(payload.get(k), str) for k in
                    ("access_token", "refresh_token", "blog")):
            raise ValueError("oauth_stored_token_invalid")
        return payload

    def write(self, tokens, blog):
        self._private_dir()
        self._safe_file(self.tokens)
        self._safe_file(self.access)
        if not isinstance(tokens.access_token, str) or not 12 <= len(tokens.access_token) <= 4096:
            raise ValueError("oauth_access_token_invalid")
        if tokens.refresh_token is not None and (not isinstance(tokens.refresh_token, str) or not 12 <= len(tokens.refresh_token) <= 4096):
            raise ValueError("oauth_refresh_token_invalid")
        payload = {"access_token": tokens.access_token,
                   "refresh_token": tokens.refresh_token or "",
                   "blog": normalize_blog(blog),
                   "expires_at": int(time.time()) + tokens.expires_in if tokens.expires_in else 0}
        # Preflight both paths before writing; no silent overwrites of unsafe files.
        self._atomic(self.tokens, json.dumps(payload, separators=(",", ":")) + "\n")
        self._atomic(self.access, tokens.access_token + "\n")

    def _atomic(self, target, content):
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=self.folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class OwnerOAuth:
    def __init__(self, directory, owner_blog, transport=None, redirect_uri=CALLBACK, clock=None):
        self.store = PrivateTokenStore(directory)
        self.blog = normalize_blog(owner_blog)
        self.transport = transport or OAuthNetwork()
        self.clock = clock or time.time
        self.oauth = None
        self.redirect_uri = redirect_uri
        self._pending = {}
        self._lock = threading.Lock()

    def _client(self):
        if self.oauth is None:
            key, secret = read_owner_client_credentials(self.store.folder)
            self.oauth = TumblrOAuth(key, secret, self.redirect_uri, self.transport)
        return self.oauth

    def start(self):
        start = self._client().begin(("basic", "offline_access"))
        now = self.clock()
        with self._lock:
            self._pending = {state: expires for state, expires in self._pending.items() if expires > now}
            if len(self._pending) >= 32:
                raise ValueError("oauth_pending_limit")
            self._pending[start.state] = now + STATE_TTL
        return start.url

    def callback(self, query):
        from urllib.parse import parse_qs
        if len(query) > 4096:
            raise ValueError("oauth_query_too_long")
        params = parse_qs(query, keep_blank_values=True)
        if len(params.get("state", [])) != 1:
            raise ValueError("oauth_state_invalid")
        state = params["state"][0]
        with self._lock:
            expires = self._pending.pop(state, 0)
        if expires <= self.clock():
            raise ValueError("oauth_state_expired_or_reused")
        client = self._client()
        tokens = client.exchange(query, state)
        identity = self.transport.get_json(USER_INFO, tokens.access_token)
        user = identity.get("response", {}).get("user") if isinstance(identity, dict) else None
        blogs = user.get("blogs") if isinstance(user, dict) else None
        if not isinstance(blogs, list) or not any(
            isinstance(blog, dict) and isinstance(blog.get("name"), str)
            and normalize_blog(blog["name"]) == self.blog for blog in blogs
        ):
            raise ValueError("oauth_expected_blog_not_owned")
        self.store.write(tokens, self.blog)
        return {"connected": True, "blog": self.blog}

    def status(self):
        record = self.store.read()
        if record is None:
            return {"connected": False}
        return {"connected": record.get("blog") == self.blog,
                "blog": self.blog if record.get("blog") == self.blog else None,
                "expires_at": record.get("expires_at", 0)}

    def refresh_if_needed(self):
        with self._lock:
            record = self.store.read()
            if record is None or record["blog"] != self.blog:
                raise ValueError("oauth_not_connected")
            expires = record.get("expires_at", 0)
            if not expires or expires > self.clock() + 120:
                return record["access_token"]
            if not record["refresh_token"]:
                raise ValueError("oauth_refresh_unavailable")
            key, secret = read_owner_client_credentials(self.store.folder)
            result = self.transport.post_form(TOKEN, {
                "grant_type": "refresh_token", "refresh_token": record["refresh_token"],
                "client_id": key, "client_secret": secret})
            token = result.get("access_token")
            refresh = result.get("refresh_token")
            lifetime = result.get("expires_in")
            if not isinstance(token, str) or not 12 <= len(token) <= 4096 or not isinstance(lifetime, int) or lifetime <= 0:
                raise ValueError("oauth_refresh_invalid")
            if not isinstance(refresh, str) or not 12 <= len(refresh) <= 4096:
                raise ValueError("oauth_refresh_invalid")
            self.store.write(AccountTokens(token, "bearer", refresh, lifetime), self.blog)
            return token
