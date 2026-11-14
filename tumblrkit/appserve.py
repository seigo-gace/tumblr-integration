"""Loopback build, serve and rollback for the Tumblr operation screen.

The screen is not returned without an injected access token. DNS, Tunnel,
Access and Secret files are outside this module.
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tumblrkit.assistant import Assistant
from tumblrkit.bridge import Bridge
from tumblrkit.tumblr_read import adapter_from_env, TumblrReadAdapter
from tumblrkit.account_runtime import OwnerOAuth, load_owner_blog, register_owner_blog
from tumblrkit.drafts import DraftStore
from tumblrkit.discovery_store import DiscoveryStore
from tumblrkit.library import Library
from tumblrkit.media_save import MediaSave, MediaError, Transcoder
from tumblrkit.attachments import AttachmentStore, AttachmentError, MAX_ATTACHMENT_BYTES, IMAGE_TYPES

COMMIT_MARK = "__TUMBLR_COMMIT__"
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")
STATIC_NAMES = ("index.html", "styles.css", "app.js")
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
COOKIE = "tg_tumblr_session"
SESSION_TTL = 12 * 60 * 60
LOGIN_CSRF_TTL = 10 * 60
LOGIN_PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="dark"><title>Tumblr操作｜ログイン</title><style>body{font-family:system-ui,sans-serif;margin:0;min-height:100vh;display:grid;place-items:center;background:#10121a;color:#f5f6fa;padding:20px}main{width:min(100%,420px);border:1px solid #3a4052;border-radius:18px;background:#1b1e29;padding:28px}h1{font-size:1.45rem}p{color:#b3bbcf;line-height:1.6}label{display:block;margin:20px 0 8px}input{width:100%;box-sizing:border-box;padding:13px;background:#10121a;color:white;border:1px solid #576eac;border-radius:10px;font:inherit}button{width:100%;margin-top:18px;padding:13px;background:#8caaff;color:#10121a;border:0;border-radius:10px;font:inherit;font-weight:700}small{display:block;margin-top:16px;color:#b3bbcf}</style></head><body><main><h1>Tumblr 操作センター</h1><p>Master専用の操作画面です。アクセス用の秘密キーを入力してください。</p><form method="post" action="/session"><label for="token">アクセスキー</label><input id="token" name="token" type="password" autocomplete="off" required minlength="8" maxlength="256"><button type="submit">ログイン</button></form><small>ログイン情報をブラウザの保存領域には記録しません。12時間でセッションが失効します。</small></main></body></html>"""


def _owner_tumblr_adapter(owner_oauth,blog,release):
    """Single reusable Owner account boundary for read and confirmed publish."""
    reader=TumblrReadAdapter(
        blog,owner_oauth.store.access,
        token_provider=owner_oauth.refresh_if_needed)
    from tumblrkit.publisher_runtime import PublisherAdapter
    draft_path=Path(release).resolve().parent.parent / "data" / "drafts.sqlite3"
    reader.publisher=PublisherAdapter(
        blog,DraftStore(draft_path),owner_oauth.publish_access_token,
        read_transport=reader.transport,budget=reader.transport.budget)
    from tumblrkit.engagement_runtime import EngagementAdapter
    reader.engagement=EngagementAdapter(
        blog,DiscoveryStore(owner_oauth.store.folder / "discovery.sqlite3"),
        owner_oauth.publish_access_token,reader.transport,reader.transport.budget)
    return reader


def _bind_owner_adapters(bridge, owner_oauth, blog, release):
    reader=_owner_tumblr_adapter(owner_oauth,blog,release)
    bridge.adapters["tumblr"]=reader
    bridge.adapters["engagement"]=reader.engagement


class ServeError(SystemExit):
    pass


def require_commit(commit):
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise ServeError("INVALID_COMMIT")
    return commit


def require_token(token):
    if not isinstance(token, str) or not 8 <= len(token) <= 256 or not all(char.isprintable() and not char.isspace() for char in token):
        raise ServeError("ACCESS_TOKEN_REQUIRED")
    return token


def loopback_host(host):
    if host == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ServeError("NON_LOOPBACK_BIND_FORBIDDEN") from None
    if not address.is_loopback:
        raise ServeError("NON_LOOPBACK_BIND_FORBIDDEN")
    return host


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repoint(link, relative):
    temporary = link.with_name(link.name + ".tmp")
    if temporary.is_symlink() or temporary.exists():
        temporary.unlink()
    temporary.symlink_to(relative)
    os.replace(temporary, link)


def _release_commit(release):
    try:
        meta = json.loads((release / "RELEASE.json").read_text(encoding="utf-8"))
        commit = require_commit(meta["commit"])
        files = meta["files"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise ServeError("INTEGRITY_MISMATCH") from None
    if not isinstance(files, dict) or set(files) != set(STATIC_NAMES):
        raise ServeError("INTEGRITY_MISMATCH")
    for name in STATIC_NAMES:
        path = release / name
        if files[name] != _sha256(path):
            raise ServeError("INTEGRITY_MISMATCH")
    html = (release / "index.html").read_text(encoding="utf-8")
    if COMMIT_MARK in html or f'data-commit="{commit}"' not in html:
        raise ServeError("INTEGRITY_MISMATCH")
    return commit


def build_release(app_dir, dest, commit):
    commit = require_commit(commit)
    app_dir = Path(app_dir)
    dest = Path(dest)
    source_html = (app_dir / "index.html").read_text(encoding="utf-8")
    if source_html.count(COMMIT_MARK) < 1:
        raise ServeError("COMMIT_MARK_MISSING")
    release = dest / "releases" / commit
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(parents=True)
    html = source_html.replace(COMMIT_MARK, commit)
    (release / "index.html").write_text(html, encoding="utf-8")
    for name in STATIC_NAMES[1:]:
        shutil.copyfile(app_dir / name, release / name)
    files = {name: _sha256(release / name) for name in STATIC_NAMES}
    (release / "RELEASE.json").write_text(
        json.dumps({"commit": commit, "files": files}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _release_commit(release)
    current = dest / "current"
    previous = dest / "previous"
    relative = os.path.join("releases", commit)
    if current.is_symlink():
        old = os.readlink(current)
        if old != relative:
            _repoint(previous, old)
    elif previous.is_symlink() or previous.exists():
        previous.unlink()
    _repoint(current, relative)
    (dest / "current-commit.txt").write_text(commit + "\n", encoding="utf-8")
    return release


def rollback_release(dest):
    dest = Path(dest)
    current = dest / "current"
    previous = dest / "previous"
    if not current.is_symlink() or not previous.is_symlink():
        raise ServeError("ROLLBACK_UNAVAILABLE")
    old = os.readlink(current)
    new = os.readlink(previous)
    if old == new or not (dest / new).is_dir():
        raise ServeError("ROLLBACK_UNAVAILABLE")
    commit = _release_commit(dest / new)
    _repoint(current, new)
    _repoint(previous, old)
    (dest / "current-commit.txt").write_text(commit + "\n", encoding="utf-8")
    return commit


def release_status(release):
    release = Path(release)
    if release.is_symlink() or release.is_dir():
        commit = _release_commit(release)
    else:
        raise ServeError("INTEGRITY_MISMATCH")
    return {"service": "tumblr-operation-app", "commit": commit, "ui": "authenticated"}


def _authorized(handler):
    now = time.time()
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        try:
            if hmac.compare_digest(header[7:].encode('utf-8'), handler.server.token.encode('utf-8')):
                return True
        except (TypeError, ValueError):
            return False
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == COOKIE:
            with handler.server.session_lock:
                expiry = handler.server.sessions.get(value, 0)
                if expiry > now:
                    return True
                handler.server.sessions.pop(value, None)
    return False


def _same_origin(handler):
    """Accept the exact public HTTPS origin through the dedicated loopback Tunnel.

    Cloudflare Tunnel may forward Host as 127.0.0.1:8081 while preserving the
    browser's original Origin. Never trust arbitrary X-Forwarded-* headers.
    """
    origin = handler.headers.get("Origin", "")
    host = handler.headers.get("Host", "").lower()
    if not origin or not host:
        return False
    parsed = urlsplit(origin)
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return False
    # Browsers control Origin; Tunnel intermediaries may rewrite Host.  The
    # server already permits only loopback peers, so require the *exact*
    # public HTTPS Origin rather than comparing it to a forwarded Host.
    if parsed.scheme == "https" and parsed.netloc.lower() == "tumblr.hentaigay.org":
        return True
    # Isolated localhost integration tests and owner-only loopback tooling.
    return (parsed.scheme == "http" and
            parsed.netloc.lower() == host and
            (host.startswith("127.0.0.1:") or host.startswith("localhost:")))


def _client_is_loopback(handler):
    try:
        return ipaddress.ip_address(handler.client_address[0]).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, status, body, content_type, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if extra:
            for key, value in extra:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, sort_keys=True), "application/json; charset=utf-8")

    def _unauthorized(self):
        self._send(401, "unauthorized\n", "text/plain; charset=utf-8")

    def _read_body(self, max_bytes=4096):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > max_bytes:
            raise ValueError("body")
        return self.rfile.read(length)

    def _presented_token(self):
        kind = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        raw = self._read_body()
        if kind == "application/json":
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"token"}:
                raise ValueError("token")
            return payload["token"], ""
        if kind == "application/x-www-form-urlencoded":
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=False, strict_parsing=True)
            if not {"token"} <= set(fields) <= {"token", "csrf"} or len(fields["token"]) != 1:
                raise ValueError("token")
            if "csrf" in fields and len(fields["csrf"]) != 1:
                raise ValueError("csrf")
            return fields["token"][0], fields.get("csrf", [""])[0]
        raise ValueError("type")

    def _session_cookie(self):
        value = secrets.token_urlsafe(32)
        with self.server.session_lock:
            self.server.sessions[value] = time.time() + SESSION_TTL
        return f"{COOKIE}={value}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}"

    def _login_page(self, status=200, error=None):
        nonce = secrets.token_urlsafe(32)
        with self.server.login_csrf_lock:
            now = time.time()
            expired = [key for key, deadline in self.server.login_csrf.items()
                       if deadline <= now]
            for key in expired:
                self.server.login_csrf.pop(key, None)
            if len(self.server.login_csrf) >= 512:
                self.server.login_csrf.clear()
            self.server.login_csrf[nonce] = now + LOGIN_CSRF_TTL
        page = LOGIN_PAGE.replace(
            "</form>",
            '<input type="hidden" name="csrf" value="' + nonce + '"></form>',
        )
        if error:
            page = page.replace(
                '<form method="post"',
                '<p role="alert" style="color:#ffb2b2">' + error + '</p><form method="post"',
            )
        self._send(status, page, "text/html; charset=utf-8")

    def _valid_form_nonce(self, nonce):
        if not isinstance(nonce, str) or not nonce:
            return False
        with self.server.login_csrf_lock:
            deadline = self.server.login_csrf.pop(nonce, 0)
        return deadline > time.time()

    def _consume_ticket(self):
        path = urlsplit(self.path).path
        prefix = "/session/ticket/"
        if not path.startswith(prefix) or not self.server.tickets:
            return False
        ticket = path[len(prefix):]
        if not _client_is_loopback(self) or ticket not in self.server.tickets:
            return False
        self.server.tickets.remove(ticket)
        self._send(302, "", "text/plain; charset=utf-8", extra=(
            ("Location", "/"),
            ("Set-Cookie", self._session_cookie()),
        ))
        return True

    def do_GET(self):
        if not _client_is_loopback(self):
            self._send(403, "forbidden\n", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        # Browser entry URLs can include harmless query parameters.
        # They must never obscure the login page with a plain-text 400.
        if parts.query and parts.path not in ("/", "/index.html", "/login", "/oauth/callback", "/library", "/media/jobs", "/drafts"):
            self._send(400, "bad request\n", "text/plain; charset=utf-8")
            return
        if parts.path == "/oauth/callback":
            if self.server.owner_oauth is None:
                self._send(503, "Tumblr連携は未設定です。", "text/plain; charset=utf-8")
                return
            try:
                linked = self.server.owner_oauth.callback(parts.query)
                with self.server.account_lock:
                    _bind_owner_adapters(
                        self.server.assistant.bridge,self.server.owner_oauth,
                        linked["blog"],self.server.release)
            except (ValueError, PermissionError, OSError, TypeError):
                self._send(400, "Tumblr認可に失敗しました。再ログイン後に連携をやり直してください。", "text/plain; charset=utf-8")
                return
            self._send(200, '<!doctype html><html lang="ja"><meta charset="utf-8"><title>Tumblr連携</title><p>Tumblrの本人確認が完了しました。</p><a href="/">操作画面に戻る</a></html>', "text/html; charset=utf-8")
            return
        if parts.path == "/campaigns":
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                from tumblrkit.core import connect as campaign_connect
                from tumblrkit.campaign_runtime import CampaignContest
                from contextlib import closing
                with closing(campaign_connect(str(self.server.draft_store.path))) as db:
                    campaigns = CampaignContest(db).list_campaigns()
            except (ValueError,sqlite3.Error,OSError):
                self._json(503, {"error":"campaign_store_unavailable"})
                return
            self._json(200, {"campaigns":campaigns})
            return
        if parts.path == "/media/jobs":
            if not _authorized(self):
                self._unauthorized()
                return
            from urllib.parse import parse_qs
            params=parse_qs(parts.query,keep_blank_values=True)
            if set(params)-{"offset","limit"} or any(len(v)!=1 for v in params.values()):
                self._json(400,{"error":"media_query_invalid"})
                return
            try:
                offset=int(params.get("offset",["0"])[0])
                limit=int(params.get("limit",["100"])[0])
                result=self.server.media_save.list(offset=offset,limit=limit)
            except (MediaError,ValueError,OverflowError):
                self._json(400,{"error":"media_query_invalid"})
                return
            except (OSError,sqlite3.Error):
                self._json(503,{"error":"media_store_unavailable"})
                return
            self._json(200,result)
            return
        if parts.path.startswith("/media/file/"):
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                filename=parts.path.rsplit("/",1)[1]
                file=self.server.media_save.file(filename)
                size=file.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type","video/mp4")
                self.send_header("Content-Length",str(size))
                self.send_header("Content-Disposition",'attachment; filename="'+filename+'.mp4"')
                self.send_header("Cache-Control","no-store")
                self.send_header("X-Content-Type-Options","nosniff")
                self.send_header("Referrer-Policy","no-referrer")
                self.end_headers()
                with open(file,"rb") as source:
                    while True:
                        chunk=source.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (MediaError,OSError):
                self._json(404,{"error":"media_file_unavailable"})
            return
        if parts.path == "/library":
            if not _authorized(self):
                self._unauthorized()
                return
            from urllib.parse import parse_qs
            params=parse_qs(parts.query,keep_blank_values=True)
            if set(params)-{"filter","offset","limit"} or any(len(v)!=1 for v in params.values()):
                self._json(400, {"error":"library_query_invalid"})
                return
            try:
                result=self.server.library.list(
                    params.get("filter",["all"])[0],
                    int(params.get("offset",["0"])[0]),
                    int(params.get("limit",["50"])[0]))
            except (ValueError,TypeError):
                self._json(400, {"error":"library_query_invalid"})
                return
            except (OSError,sqlite3.Error):
                self._json(503, {"error":"library_unavailable"})
                return
            self._json(200,result)
            return
        if parts.path == "/discovery/candidates":
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                rows = self.server.discovery_store.read_candidates(limit=100)
                blogs = self.server.discovery_store.read_blogs(limit=100)
            except (ValueError, OSError, sqlite3.Error):
                self._json(503, {"error": "discovery_store_unavailable"})
                return
            self._json(200, {"posts": rows, "blogs": blogs})
            return
        if parts.path == "/oauth/status":
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                result = self.server.owner_oauth.status() if self.server.owner_oauth else {"connected": False, "configured": False}
            except (ValueError, OSError, TypeError):
                result = {"connected": False}
            self._json(200, result)
            return
        if parts.path.startswith("/attachments/"):
            if not _authorized(self):
                self._unauthorized()
                return
            try:
                item = self.server.attachment_store.get(parts.path[len("/attachments/"):])
            except (AttachmentError, OSError):
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            if item is None:
                self._send(404, "not found\n", "text/plain; charset=utf-8")
                return
            is_image = item["media_type"] in IMAGE_TYPES
            # Display images on the same authenticated origin, force download
            # for all other file types. Never inline HTML/SVG or unknown bytes.
            safe_filename = "attachment-" + item["id"]
            disposition = ("inline" if is_image else "attachment") + '; filename="' + safe_filename + '"'
            self._send(200, item["data"], item["media_type"],
                       extra=[("Content-Disposition", disposition)])
            return
        if parts.path == "/drafts":
            if not _authorized(self):
                self._unauthorized()
                return
            from urllib.parse import parse_qs
            params=parse_qs(parts.query,keep_blank_values=True)
            if set(params)-{"offset","limit"} or any(len(v)!=1 for v in params.values()):
                self._json(400,{"error":"invalid_draft_page"})
                return
            try:
                offset=int(params.get("offset",["0"])[0])
                limit=int(params.get("limit",["100"])[0])
                drafts=self.server.draft_store.list(offset=offset,limit=limit)
                total=self.server.draft_store.count()
            except ValueError:
                self._json(400,{"error":"invalid_draft_page"})
                return
            except (OSError,sqlite3.Error):
                self._json(503,{"error":"draft_store_unavailable"})
                return
            self._json(200,{"drafts":drafts,"total":total,"offset":offset,"limit":limit,
                             "has_next":offset+len(drafts)<total})
            return
        if parts.path in ("/healthz", "/version"):
            self._json(200, release_status(self.server.release))
            return
        if parts.path == "/login":
            self._login_page()
            return
        if self._consume_ticket():
            return
        if parts.path.startswith("/session/ticket/"):
            self._unauthorized()
            return
        name = "index.html" if parts.path in ("/", "/index.html") else parts.path[1:]
        if name not in STATIC_NAMES or not _authorized(self):
            self._login_page(401) if name == "index.html" else (self._unauthorized() if name in STATIC_NAMES else self._send(404, "not found\n", "text/plain; charset=utf-8"))
            return
        self._send(200, (self.server.release / name).read_bytes(), CONTENT_TYPES[name])

    def do_POST(self):
        if not _client_is_loopback(self):
            self._send(403, "forbidden\n", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        if parts.query:
            self._send(400, "bad request\n", "text/plain; charset=utf-8")
            return
        if parts.path == "/campaigns":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization","").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\\n", "text/plain; charset=utf-8")
                return
            try:
                from contextlib import closing
                from tumblrkit.core import connect as campaign_connect
                from tumblrkit.campaign_runtime import CampaignContest
                if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/json":
                    raise ValueError("campaign_request_invalid")
                data=json.loads(self._read_body(4096).decode("utf-8"))
                if not isinstance(data,dict) or not isinstance(data.get("action"),str):
                    raise ValueError("campaign_request_invalid")
                action=data["action"]
                with closing(campaign_connect(str(self.server.draft_store.path))) as db:
                    owner=CampaignContest(db)
                    if action=="create" and set(data)=={"action","campaign_id","prize","deadline","winners","claim_seconds"}:
                        response=owner.create(data["campaign_id"],data["prize"],
                                              deadline=data["deadline"],winners=data["winners"],
                                              claim_seconds=data["claim_seconds"])
                    elif action=="draw" and set(data)=={"action","campaign_id"}:
                        result=owner.draw(data["campaign_id"])
                        response={"campaign_id":result.campaign_id,"commitment":result.commitment,
                                  "winners":list(result.winners),"participants":result.participants,
                                  "winner_count":result.winner_count}
                    elif action=="promote" and set(data)=={"action","campaign_id"}:
                        response={"promoted":owner.promote_expired(data["campaign_id"])}
                    elif action=="close" and set(data)=={"action","campaign_id"}:
                        response={"state":owner.close(data["campaign_id"])}
                    else:
                        raise ValueError("campaign_request_invalid")
            except (ValueError,TypeError,OverflowError,UnicodeError,json.JSONDecodeError) as exc:
                self._json(400,{"error":str(exc) if str(exc).startswith("campaign_") else "campaign_request_invalid"})
                return
            except (OSError,sqlite3.Error):
                self._json(503,{"error":"campaign_store_unavailable"})
                return
            self._json(200,response)
            return
        if parts.path == "/oauth/configure":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            try:
                payload = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"blog"}:
                    raise ValueError("oauth_blog_invalid")
                with self.server.account_lock:
                    blog = register_owner_blog(self.server.oauth_folder, payload["blog"])
                    if self.server.owner_oauth is None:
                        self.server.owner_oauth = OwnerOAuth(self.server.oauth_folder, blog)
                        _bind_owner_adapters(
                            self.server.assistant.bridge,self.server.owner_oauth,
                            blog,self.server.release)
                    elif self.server.owner_oauth.blog != blog:
                        # Changed unconnected blog; use the same existing account adapter boundary.
                        self.server.owner_oauth = OwnerOAuth(self.server.oauth_folder, blog)
                        _bind_owner_adapters(
                            self.server.assistant.bridge,self.server.owner_oauth,
                            blog,self.server.release)
            except (ValueError, OSError, TypeError, UnicodeError, json.JSONDecodeError):
                self._json(400, {"error": "oauth_blog_setup_failed"})
                return
            self._json(200, {"configured": True, "blog": blog})
            return
        if parts.path == "/media/save":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok=self.headers.get("Authorization","").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403,"origin rejected\n","text/plain; charset=utf-8")
                return
            try:
                if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/json":
                    raise MediaError("MEDIA_REQUEST_INVALID")
                request=json.loads(self._read_body(2048).decode("utf-8"))
                if not isinstance(request,dict) or set(request)!={"candidate_id","media_index","rights_confirmed"}:
                    raise MediaError("MEDIA_REQUEST_INVALID")
                data=self.server.media_save.save(request["candidate_id"],request["media_index"],
                                                 request["rights_confirmed"])
                self._json(200,data)
            except (ValueError,TypeError,UnicodeError,json.JSONDecodeError) as exc:
                error=str(exc) if isinstance(exc,MediaError) else "MEDIA_REQUEST_INVALID"
                if error in ("MEDIA_DISK_FULL","MEDIA_QUOTA_EXCEEDED","MEDIA_BUSY",
                             "MEDIA_JOB_IN_PROGRESS"):
                    self._json(409,{"error":error})
                elif error in ("MEDIA_HTTP_UNAVAILABLE","MEDIA_HEAD_UNSUPPORTED","MEDIA_TYPE_UNSUPPORTED",
                               "MEDIA_EXTRACTOR_NOT_FOUND","MEDIA_EXTRACTOR_UNAVAILABLE",
                               "MEDIA_ALL_METHODS_EXHAUSTED","MEDIA_PROVIDER_REFUSED",
                               "MEDIA_EXTRACTOR_AMBIGUOUS","MEDIA_POST_INVALID","MEDIA_EXTRACTOR_FAILED",
                               "MEDIA_EXTRACTOR_UNSAFE_FILE","MEDIA_LENGTH_UNKNOWN",
                               "MEDIA_SIZE_EXCEEDED","MEDIA_SIZE_CHANGED","MEDIA_DESTINATION_UNSAFE",
                               "MEDIA_CONNECT_UNAVAILABLE","MEDIA_PROBE_FAILED","MEDIA_PROBE_UNAVAILABLE",
                               "MEDIA_CONVERSION_FAILED","MEDIA_CONVERSION_UNAVAILABLE",
                               "MEDIA_OUTPUT_NOT_MP4","MEDIA_OUTPUT_SIZE_EXCEEDED","MEDIA_PROCESS_FAILED"):
                    self._json(422,{"error":error})
                else:
                    self._json(400,{"error":error})
            except (OSError,sqlite3.Error):
                self._json(503,{"error":"media_store_unavailable"})
            return
        if parts.path in ("/library/update","/library/progress","/library/selected"):
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok=self.headers.get("Authorization","").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403,"origin rejected\n","text/plain; charset=utf-8")
                return
            try:
                if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/json":
                    raise ValueError("invalid_content_type")
                data=json.loads(self._read_body(65536).decode("utf-8"))
                if not isinstance(data,dict):
                    raise ValueError("library_payload_invalid")
                if parts.path=="/library/update":
                    if set(data)!={"candidate_ids","field","value"}:
                        raise ValueError("library_payload_invalid")
                    result=self.server.library.update(data["candidate_ids"],data["field"],data["value"])
                elif parts.path=="/library/selected":
                    if set(data)!={"candidate_ids"}:
                        raise ValueError("library_payload_invalid")
                    result=self.server.library.selected(data["candidate_ids"])
                else:
                    if set(data)!={"candidate_id","media_index"}:
                        raise ValueError("library_payload_invalid")
                    result=self.server.library.progress(data["candidate_id"],data["media_index"])
            except (ValueError,TypeError,UnicodeError,json.JSONDecodeError):
                self._json(400,{"error":"library_request_invalid"})
                return
            except (OSError,sqlite3.Error):
                self._json(503,{"error":"library_unavailable"})
                return
            self._json(200,result)
            return
        if parts.path == "/discovery/recheck":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            try:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("invalid_content_type")
                data = json.loads(self._read_body(512).decode("utf-8"))
                candidate = data.get("candidate_id") if isinstance(data, dict) and set(data) == {"candidate_id"} else None
                if not isinstance(candidate, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,25}",candidate):
                    raise ValueError("invalid_candidate")
                adapter = self.server.assistant.bridge.adapters.get("tumblr")
                if not isinstance(adapter, TumblrReadAdapter):
                    self._json(503, {"error": "tumblr_not_connected"})
                    return
                from tumblrkit.discovery import DiscoveryEngine
                result = DiscoveryEngine(adapter.blog,adapter.token_file,adapter.transport,
                                         token_provider=adapter.token_provider).recheck_candidate(candidate)
            except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                code = str(exc)
                if code in ("tumblr_budget_cooldown", "tumblr_budget_exhausted", "tumblr_rate_limited"):
                    self._json(429, {"error": "tumblr_read_cooldown"})
                elif code in ("tumblr_unavailable", "tumblr_post_unavailable", "discovery_candidate_check_unverified",
                              "tumblr_response_invalid", "tumblr_budget_unavailable"):
                    self._json(503, {"error": "discovery_recheck_unavailable"})
                else:
                    self._json(400, {"error": "discovery_candidate_invalid"})
                return
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "discovery_recheck_unavailable"})
                return
            self._json(200, result)
            return
        if parts.path == "/discovery/exclude":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            try:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise ValueError("invalid_content_type")
                data = json.loads(self._read_body(512).decode("utf-8"))
                if not isinstance(data, dict) or set(data) != {"blog"}:
                    raise ValueError("invalid_discovery_payload")
                self.server.discovery_store.exclude_blog(data["blog"])
            except (ValueError, UnicodeError, TypeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_discovery_exclusion"})
                return
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "discovery_store_unavailable"})
                return
            self._json(200, {"excluded": True})
            return
        if parts.path == "/oauth/start":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            if self.server.owner_oauth is None:
                self._json(503, {"error": "oauth_not_configured"})
                return
            try:
                url = self.server.owner_oauth.start()
            except (ValueError, OSError):
                self._json(503, {"error": "oauth_setup_unavailable"})
                return
            self._json(200, {"authorize_url": url})
            return
        if parts.path == "/attachments":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            try:
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise AttachmentError("INVALID_CONTENT_TYPE")
                payload = json.loads(self._read_body(11 * 1024 * 1024).decode("utf-8"))
                if not isinstance(payload, dict) or set(payload) != {"name", "data"}:
                    raise AttachmentError("INVALID_ATTACHMENT_PAYLOAD")
                encoded = payload["data"]
                if not isinstance(encoded, str) or len(encoded) > (MAX_ATTACHMENT_BYTES * 4 // 3) + 8:
                    raise AttachmentError("INVALID_FILE_SIZE")
                binary = base64.b64decode(encoded, validate=True)
                record = self.server.attachment_store.save(payload["name"], binary)
            except (AttachmentError, ValueError, UnicodeError, TypeError, binascii.Error, json.JSONDecodeError):
                self._json(400, {"error": "invalid_attachment"})
                return
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "attachment_store_unavailable"})
                return
            self._json(201, record)
            return
        if parts.path == "/drafts":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            try:
                data = json.loads(self._read_body().decode("utf-8"))
                if not isinstance(data, dict) or set(data) != {"text"}:
                    raise ValueError("payload")
                record = self.server.draft_store.save(data["text"])
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_draft"})
                return
            except OSError:
                self._json(503, {"error": "draft_store_unavailable"})
                return
            self._json(201, record)
            return
        if parts.path == "/assistant":
            if not _authorized(self):
                self._unauthorized()
                return
            bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
            if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
                self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
                return
            self._assistant()
            return
        if parts.path != "/session":
            self._send(404, "not found\n", "text/plain; charset=utf-8")
            return
        try:
            presented, nonce = self._presented_token()
        except (ValueError, json.JSONDecodeError, UnicodeError, TypeError):
            self._login_page(400, "入力形式を確認してください。")
            return
        csrf_ok = self._valid_form_nonce(nonce)
        origin_rejected = bool(self.headers.get("Origin")) and not _same_origin(self)
        fetch_rejected = self.headers.get("Sec-Fetch-Site", "") == "cross-site"
        if (origin_rejected or fetch_rejected) and not csrf_ok:
            self._login_page(403, "ページを開き直してログインしてください。")
            return
        try:
            valid_token = isinstance(presented, str) and hmac.compare_digest(
                presented.encode("utf-8"), self.server.token.encode("utf-8")
            )
        except (UnicodeError, TypeError):
            valid_token = False
        if not valid_token:
            self._login_page(401, "ログインキーが一致しません。")
            return
        cookie = self._session_cookie()
        if "text/html" in self.headers.get("Accept", ""):
            self._send(303, b"", "text/plain; charset=utf-8",
                       extra=(("Location", "/"), ("Set-Cookie", cookie)))
        else:
            self._send(204, b"", "text/plain; charset=utf-8", extra=(("Set-Cookie", cookie),))

    def do_PUT(self):
        if not _client_is_loopback(self):
            self._send(403,"forbidden\n","text/plain; charset=utf-8")
            return
        if not _authorized(self):
            self._unauthorized()
            return
        bearer_ok = self.headers.get("Authorization","").startswith("Bearer ") and _authorized(self)
        if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
            self._send(403,"origin rejected\n","text/plain; charset=utf-8")
            return
        parts=urlsplit(self.path)
        if parts.query or not parts.path.startswith("/drafts/"):
            self._send(404,"not found\n","text/plain; charset=utf-8")
            return
        try:
            if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/json":
                raise ValueError("INVALID_DRAFT")
            data=json.loads(self._read_body().decode("utf-8"))
            if not isinstance(data,dict) or set(data)!={"text","expected_text"}:
                raise ValueError("INVALID_DRAFT")
            result=self.server.draft_store.update(
                parts.path[len("/drafts/"):],data["text"],data["expected_text"])
        except ValueError as exc:
            code=str(exc)
            if code in ("DRAFT_EDIT_CONFLICT","DRAFT_PUBLISH_LOCKED"):
                self._json(409,{"error":code.lower()})
            elif code=="DRAFT_NOT_FOUND":
                self._json(404,{"error":"draft_not_found"})
            else:
                self._json(400,{"error":"invalid_draft"})
            return
        except (UnicodeError,TypeError,json.JSONDecodeError):
            self._json(400,{"error":"invalid_draft"})
            return
        except (OSError,sqlite3.Error):
            self._json(503,{"error":"draft_store_unavailable"})
            return
        self._json(200,result)

    def do_DELETE(self):
        if not _client_is_loopback(self):
            self._send(403, "forbidden\n", "text/plain; charset=utf-8")
            return
        if not _authorized(self):
            self._unauthorized()
            return
        bearer_ok = self.headers.get("Authorization", "").startswith("Bearer ") and _authorized(self)
        if (self.headers.get("Origin") and not _same_origin(self)) or (not self.headers.get("Origin") and not bearer_ok):
            self._send(403, "origin rejected\n", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        if parts.path.startswith("/attachments/") and not parts.query:
            try:
                deleted = self.server.attachment_store.delete(parts.path[len("/attachments/"):])
            except AttachmentError:
                self._json(400, {"error": "invalid_attachment"})
                return
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "attachment_store_unavailable"})
                return
            self._json(200, {"deleted": deleted})
            return
        if parts.query or not parts.path.startswith("/drafts/"):
            self._send(404, "not found\n", "text/plain; charset=utf-8")
            return
        try:
            deleted = self.server.draft_store.delete(parts.path[len("/drafts/"):])
        except ValueError as exc:
            if str(exc)=="DRAFT_PUBLISH_LOCKED":
                self._json(409, {"error":"draft_publish_locked"})
            else:
                self._json(400, {"error":"invalid_draft"})
            return
        except (OSError,sqlite3.Error):
            self._json(503, {"error": "draft_store_unavailable"})
            return
        self._json(200, {"deleted": deleted})

    def _assistant(self):
        if not _authorized(self):
            self._unauthorized()
            return
        try:
            payload = json.loads(self._read_body().decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeError):
            self._send(400, "bad request\n", "text/plain; charset=utf-8")
            return
        if not isinstance(payload, dict):
            self._send(400, "bad request\n", "text/plain; charset=utf-8")
            return
        message = payload.get("message") if isinstance(payload.get("message"), str) else ""
        request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
        attachments = payload.get("attachments", [])
        try:
            records = self.server.attachment_store.describe(attachments)
        except (AttachmentError, OSError, sqlite3.Error, TypeError):
            self._json(400, {"error": "invalid_attachment_reference"})
            return
        # Owner-only CM commands reuse the existing private drafts SQLite.
        # Preview/management never sends a Telegram message.
        if message.strip().startswith(("CM一覧", "CMを追加", "このCMを停止",
                                       "CMを再開", "次のCMを見る")):
            from tumblrkit.cm_links import CMRegistry, handle_cm_message
            try:
                db = self.server.draft_store._connect()
                try:
                    with db:
                        response = handle_cm_message(CMRegistry(db), message)
                finally:
                    db.close()
            except (ValueError, TypeError):
                self._json(400, {"error": "cm_request_invalid"})
                return
            except (OSError, sqlite3.Error):
                self._json(503, {"error": "cm_store_unavailable"})
                return
            if response is not None:
                state, text = response
                self._json(200, self.server.assistant._pack(state, text, False))
                return
        # The current Assistant has no multimodal adapter. File bytes remain
        # private on Gserver; acknowledge receipt without pretending analysis.
        if records:
            result = self.server.assistant._pack(
                "attachments_received",
                str(len(records)) + "件の写真・ファイルを受け取りました。"
                "現在はAIの画像・文書解析が未接続のため、内容を解析・公開していません。",
                False,
            )
            result["attachments"] = records
            self._json(200, result)
            return
        self._json(200, self.server.assistant.turn(
            message=message,
            confirm=payload.get("confirm") is True,
            request_id=request_id,
            draft=payload.get("draft") is True,
            abort=payload.get("abort") is True,
            goals=payload.get("goals", []),
            selected_action=payload.get("selected_action"),
            selected_target=payload.get("selected_target"),
        ))


class AppServer:
    def __init__(self, release, token, host="127.0.0.1", port=0, ticket=False, assistant=None, telegram_adapter=None):
        self.release = Path(release)
        self.token = require_token(token)
        self.host = loopback_host(host)
        self.port = port
        self.ticket = ticket
        self.owner_oauth = None
        if assistant is not None:
            self.assistant = assistant
        else:
            self.oauth_folder = Path.home() / ".config" / "tgtumbler" / "tumblr-oauth"
            owner_blog = os.environ.get("TUMBLR_OWNER_BLOG", "").strip()
            if not owner_blog and self.oauth_folder.is_dir():
                try:
                    owner_blog = load_owner_blog(self.oauth_folder) or ""
                except (ValueError, OSError):
                    owner_blog = ""
            self.owner_oauth = OwnerOAuth(self.oauth_folder, owner_blog or None)
            selected_blog = owner_blog
            if not selected_blog:
                try:
                    saved = self.owner_oauth.store.read()
                    selected_blog = saved.get("blog", "") if saved else ""
                    if selected_blog:
                        self.owner_oauth.blog = selected_blog
                except (ValueError, OSError, TypeError):
                    selected_blog = ""
            if selected_blog:
                tumblr_reader = _owner_tumblr_adapter(
                    self.owner_oauth,selected_blog,self.release)
            else:
                tumblr_reader = adapter_from_env()
            from tumblrkit.analytics_runtime import AnalyticsAdapter
            analytics=AnalyticsAdapter(
                DiscoveryStore(self.oauth_folder / "discovery.sqlite3"),
                campaign_db_path=self.release.resolve().parent.parent / "data" / "drafts.sqlite3")
            adapters={"analytics": analytics}
            if telegram_adapter is None:
                from tumblrkit.telegram_bridge_client import telegram_owner_from_env
                telegram_adapter = telegram_owner_from_env(
                    cm_store_path=self.release.resolve().parent.parent / "data" / "drafts.sqlite3")
            if telegram_adapter is not None:
                adapters["telegram"] = telegram_adapter
            if tumblr_reader:
                adapters["tumblr"]=tumblr_reader
                if getattr(tumblr_reader,"engagement",None) is not None:
                    adapters["engagement"]=tumblr_reader.engagement
            self.assistant = Assistant(bridge=Bridge(adapters))
        self.ticket_path = None
        self.httpd = None
        self.thread = None

    def __enter__(self):
        _release_commit(self.release)
        self.httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.httpd.release = self.release
        self.httpd.token = self.token
        self.httpd.tickets = set()
        self.httpd.sessions = {}
        self.httpd.session_lock = threading.Lock()
        self.httpd.login_csrf = {}
        self.httpd.login_csrf_lock = threading.Lock()
        self.httpd.assistant = self.assistant
        self.httpd.owner_oauth = self.owner_oauth
        self.httpd.oauth_folder = getattr(self, 'oauth_folder', Path.home() / '.config' / 'tgtumbler' / 'tumblr-oauth')
        self.httpd.account_lock = threading.Lock()
        self.httpd.discovery_store = DiscoveryStore(
            Path.home() / ".config" / "tgtumbler" / "tumblr-oauth" / "discovery.sqlite3")
        self.httpd.library = Library(self.httpd.discovery_store)
        media_root=self.release.resolve().parent.parent / "data" / "media"
        media_root.mkdir(parents=True,exist_ok=True,mode=0o700)
        tools_dir=media_root.parent / "media-tools"
        # Paths are resolved when a user explicitly saves: installing approved
        # app-only tools later never requires restarting shared Gserver services.
        converter=Transcoder(str(tools_dir / "ffprobe"),str(tools_dir / "ffmpeg"))
        self.httpd.media_save = MediaSave(self.httpd.discovery_store,media_root,transcoder=converter)
        self.httpd.draft_store = DraftStore(self.release.resolve().parent.parent / "data" / "drafts.sqlite3")
        # Campaign/Referral schema is kept in the existing private drafts DB.
        # There is no new database or second posting service.
        from tumblrkit.core import connect as campaign_connect
        campaign_db = campaign_connect(str(self.httpd.draft_store.path))
        from tumblrkit.campaign_runtime import CampaignContest
        CampaignContest(campaign_db)
        campaign_db.close()
        # Assistant and the owner UI must use the same durable draft records.
        self.httpd.assistant.draft_store = self.httpd.draft_store
        self.httpd.attachment_store = AttachmentStore(self.release.resolve().parent.parent / "data")
        if self.ticket:
            ticket = secrets.token_urlsafe(24)
            self.httpd.tickets.add(ticket)
            self.ticket_path = "/session/ticket/" + ticket
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        return False


def _cmd_build(args):
    build_release(args.app, args.dest, args.commit)
    print("BUILD_COMMIT=" + args.commit)


def _cmd_rollback(args):
    print("ROLLBACK_COMMIT=" + rollback_release(args.dest))


def _cmd_status(args):
    print(json.dumps(release_status(args.release), sort_keys=True))


def _cmd_serve(args):
    token = require_token(os.environ.get("TUMBLR_APP_ACCESS_TOKEN", ""))
    ticket = os.environ.get("TUMBLR_APP_LOOPBACK_TICKET") == "1"
    with AppServer(args.release, token, args.bind, args.port, ticket=ticket) as server:
        if server.ticket_path:
            print(f"LOOPBACK_TICKET=http://{server.host}:{server.port}{server.ticket_path}", flush=True)
        else:
            print(f"SERVING=http://{server.host}:{server.port}", flush=True)
        try:
            server.thread.join()
        except KeyboardInterrupt:
            return


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tumblrkit.appserve")
    commands = parser.add_subparsers(dest="cmd", required=True)
    build = commands.add_parser("build")
    build.add_argument("--app", type=Path, default=Path("app"))
    build.add_argument("--dest", type=Path, default=Path("dist"))
    build.add_argument("--commit", required=True)
    build.set_defaults(func=_cmd_build)
    serve = commands.add_parser("serve")
    serve.add_argument("--release", type=Path, default=Path("dist/current"))
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("TUMBLR_APP_PORT", "8081")))
    serve.set_defaults(func=_cmd_serve)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--dest", type=Path, default=Path("dist"))
    rollback.set_defaults(func=_cmd_rollback)
    status = commands.add_parser("status")
    status.add_argument("--release", type=Path, default=Path("dist/current"))
    status.set_defaults(func=_cmd_status)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
