"""Read-only Tumblr post adapter for the existing Assistant bridge.

It uses a separate owner-approved OAuth token file, never the operation-app login
secret. Unconfigured access fails closed. All API destinations are fixed.
"""
import json
import os
import re
import stat
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from tumblrkit.api_safety import TumblrReadBudget

BLOG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}(?:\.tumblr\.com)?$", re.I)
API = "https://api.tumblr.com"
MAX_REPLY = 1024 * 1024


def normalize_blog(value):
    if not isinstance(value, str):
        raise ValueError("blog_invalid")
    blog = value.strip().lower()
    if not BLOG.fullmatch(blog):
        raise ValueError("blog_invalid")
    return blog.removesuffix(".tumblr.com")


def token_from_file(path_value):
    path = Path(path_value).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("oauth_token_file_unavailable")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("oauth_token_permissions")
    token = path.read_text(encoding="utf-8").strip()
    if not 12 <= len(token) <= 4096 or any(char.isspace() for char in token):
        raise ValueError("oauth_token_invalid")
    return token


class OfficialTransport:
    def __init__(self, budget):
        self.budget = budget

    def get(self, path, bearer):
        if not path.startswith("/v2/") or "//" in path:
            raise ValueError("api_path_invalid")
        request = Request(
            API + path,
            headers={"Authorization": "Bearer " + bearer,
                     "User-Agent": "TGtumbler-Operation-Assistant/1.0",
                     "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=12) as response:
                if response.status != 200:
                    raise ValueError("tumblr_http_status")
                raw = response.read(MAX_REPLY + 1)
        except HTTPError as exc:
            if exc.code == 429:
                self.budget.pause_after_limit()
                raise ValueError("tumblr_rate_limited") from None
            raise ValueError("tumblr_unavailable") from None
        except (URLError, OSError, TimeoutError):
            raise ValueError("tumblr_unavailable") from None
        if len(raw) > MAX_REPLY:
            raise ValueError("tumblr_response_too_large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("tumblr_response_invalid") from None


def media_thumbnail(post):
    """Extract a Tumblr-hosted preview from NPF image/video blocks, if present."""
    blocks = post.get("content") if isinstance(post, dict) else None
    if not isinstance(blocks, list):
        return "", ""
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") not in ("image", "video"):
            continue
        kind = block["type"]
        media = block.get("poster") if kind == "video" else block.get("media")
        if isinstance(media, dict):
            media = [media]
        if not isinstance(media, list):
            continue
        for item in media:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or len(url) > 500:
                continue
            parsed = urlsplit(url)
            hostname = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and not parsed.username and not parsed.password and (
                hostname == "media.tumblr.com" or hostname.endswith(".media.tumblr.com")
            ):
                return url, kind
    return "", ""


class TumblrReadAdapter:
    def __init__(self, owner_blog, token_file, transport=None, token_provider=None):
        self.blog = normalize_blog(owner_blog)
        self.token_file = token_file
        self.transport = transport or OfficialTransport(
            TumblrReadBudget(Path(token_file).expanduser().parent / "tumblr-api-budget.sqlite3"))
        self.token_provider = token_provider

    def perform(self, action, target, payload):
        if action != "tumblr.list_posts":
            raise ValueError("read_action_not_connected")
        if normalize_blog(target) != self.blog:
            raise ValueError("non_owner_blog")
        if isinstance(self.transport, OfficialTransport):
            self.transport.budget.reserve(2)
        bearer = self.token_provider() if self.token_provider else token_from_file(self.token_file)
        identity = self.transport.get("/v2/user/info", bearer)
        user = identity.get("response", {}).get("user") if isinstance(identity, dict) else None
        blogs = user.get("blogs") if isinstance(user, dict) else None
        if not isinstance(blogs, list) or not any(
            isinstance(blog, dict) and normalize_blog(blog.get("name", "")) == self.blog
            for blog in blogs
        ):
            raise ValueError("oauth_owner_not_verified")
        path = "/v2/blog/" + quote(self.blog + ".tumblr.com", safe="") + "/posts?npf=true&limit=20"
        response = self.transport.get(path, bearer)
        if not isinstance(response, dict) or response.get("meta", {}).get("status") != 200:
            raise ValueError("tumblr_posts_unverified")
        data = response.get("response")
        posts = data.get("posts") if isinstance(data, dict) else None
        blog_info = data.get("blog") if isinstance(data, dict) else None
        if not isinstance(posts, list) or not isinstance(blog_info, dict) or normalize_blog(blog_info.get("name", "")) != self.blog:
            raise ValueError("tumblr_posts_unverified")
        rows = []
        for item in posts[:20]:
            if not isinstance(item, dict):
                continue
            raw_url = item.get("post_url")
            url = raw_url if isinstance(raw_url, str) and raw_url.startswith("https://") else ""
            name = item.get("summary") or item.get("title") or "(タイトルなし)"
            thumbnail, npf_type = media_thumbnail(item)
            rows.append({
                "title": str(name)[:200],
                "type": str(npf_type or item.get("type") or "")[:40],
                "date": str(item.get("date") or "")[:60],
                "thumbnail_url": thumbnail,
                "url": url[:500],
            })
        return {"verified": True, "rows": rows, "total": len(posts)}


def adapter_from_env(env=None):
    env = env if env is not None else os.environ
    owner = env.get("TUMBLR_OWNER_BLOG", "").strip()
    token_file = env.get("TUMBLR_OAUTH_ACCESS_TOKEN_FILE", "").strip()
    if not owner or not token_file:
        return None
    try:
        token_from_file(token_file)
        return TumblrReadAdapter(owner, token_file)
    except (ValueError, OSError):
        return None
