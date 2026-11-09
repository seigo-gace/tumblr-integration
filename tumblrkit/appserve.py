"""Loopback build, serve and rollback for the Tumblr operation screen.

The screen is not returned without an injected access token. DNS, Tunnel,
Access and Secret files are outside this module.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from tumblrkit.assistant import Assistant

COMMIT_MARK = "__TUMBLR_COMMIT__"
COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")
STATIC_NAMES = ("index.html", "styles.css", "app.js")
CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
COOKIE = "tg_tumblr_access"


class ServeError(SystemExit):
    pass


def require_commit(commit):
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise ServeError("INVALID_COMMIT")
    return commit


def require_token(token):
    if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9._~+/-]{16,256}", token) is None:
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
    header = handler.headers.get("Authorization", "")
    token = handler.server.token
    if header.startswith("Bearer "):
        presented = header[7:]
        try:
            if hmac.compare_digest(presented, token):
                return True
        except (TypeError, ValueError):
            return False
    cookie = handler.headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == COOKIE:
            try:
                if hmac.compare_digest(value, token):
                    return True
            except (TypeError, ValueError):
                return False
    return False


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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if extra:
            for key, value in extra:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, payload):
        self._send(status, json.dumps(payload, sort_keys=True), "application/json; charset=utf-8")

    def _unauthorized(self):
        self._send(401, "unauthorized\n", "text/plain; charset=utf-8")

    def _read_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > 4096:
            raise ValueError("body")
        return self.rfile.read(length)

    def _presented_token(self):
        kind = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        raw = self._read_body()
        if kind == "application/json":
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"token"}:
                raise ValueError("token")
            return payload["token"]
        if kind == "application/x-www-form-urlencoded":
            fields = parse_qs(raw.decode("utf-8"), keep_blank_values=False, strict_parsing=True)
            if set(fields) != {"token"} or len(fields["token"]) != 1:
                raise ValueError("token")
            return fields["token"][0]
        raise ValueError("type")

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
            ("Set-Cookie", f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"),
        ))
        return True

    def do_GET(self):
        if not _client_is_loopback(self):
            self._send(403, "forbidden\n", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        if parts.query or parts.fragment:
            self._send(400, "bad request\n", "text/plain; charset=utf-8")
            return
        if parts.path in ("/healthz", "/version"):
            self._json(200, release_status(self.server.release))
            return
        if self._consume_ticket():
            return
        if parts.path.startswith("/session/ticket/"):
            self._unauthorized()
            return
        name = "index.html" if parts.path in ("/", "/index.html") else parts.path[1:]
        if name not in STATIC_NAMES or not _authorized(self):
            self._unauthorized() if name in STATIC_NAMES or parts.path == "/" else self._send(404, "not found\n", "text/plain; charset=utf-8")
            return
        self._send(200, (self.server.release / name).read_bytes(), CONTENT_TYPES[name])

    def do_POST(self):
        if not _client_is_loopback(self):
            self._send(403, "forbidden\n", "text/plain; charset=utf-8")
            return
        parts = urlsplit(self.path)
        if parts.path == "/assistant":
            self._assistant()
            return
        if parts.path != "/session" or parts.query:
            self._send(404, "not found\n", "text/plain; charset=utf-8")
            return
        try:
            presented = self._presented_token()
            ok = isinstance(presented, str) and hmac.compare_digest(presented, self.server.token)
        except (ValueError, json.JSONDecodeError, UnicodeError, TypeError):
            ok = False
        if not ok:
            self._unauthorized()
            return
        self._send(204, b"", "text/plain; charset=utf-8", extra=(
            ("Set-Cookie", f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"),
        ))

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
        self._json(200, self.server.assistant.turn(
            message=message,
            confirm=payload.get("confirm") is True,
            request_id=request_id,
            draft=payload.get("draft") is True,
            abort=payload.get("abort") is True,
        ))


class AppServer:
    def __init__(self, release, token, host="127.0.0.1", port=0, ticket=False, assistant=None):
        self.release = Path(release)
        self.token = require_token(token)
        self.host = loopback_host(host)
        self.port = port
        self.ticket = ticket
        self.assistant = assistant if assistant is not None else Assistant()
        self.ticket_path = None
        self.httpd = None
        self.thread = None

    def __enter__(self):
        _release_commit(self.release)
        self.httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self.httpd.release = self.release
        self.httpd.token = self.token
        self.httpd.tickets = set()
        self.httpd.assistant = self.assistant
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
