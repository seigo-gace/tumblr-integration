"""Deploy *only* the existing Tumblr operation app with a private owner token file.

No credential reads from /proc, no token logging, no Cloudflare/Telegram changes,
no AI provider requests. This is a direct Gserver owner operation, not CI.
"""
import argparse
import http.client
import json
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

# Invoked as python scripts/tumblr_release.py: include repository root for imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET_BRANCH = "feat/initial-modular-core"
PRODUCTION_PORT = 8081
STAGING_PORT = 8082


class ReleaseError(Exception):
    pass


def source_token(token_file):
    path = Path(token_file).expanduser()
    if not path.is_file() or path.is_symlink():
        raise ReleaseError("OWNER_TOKEN_FILE_MISSING")
    if path.stat().st_mode & 0o077:
        raise ReleaseError("OWNER_TOKEN_FILE_PERMISSIONS")
    value = path.read_text(encoding="utf-8").strip()
    from tumblrkit.appserve import require_token
    require_token(value)
    return value


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode:
        raise ReleaseError("SOURCE_GIT_FAILED")
    return result.stdout.strip()


def request(port, path, token=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    headers = {"Authorization": "Bearer " + token} if token else {}
    try:
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(65536)
    finally:
        conn.close()


def verify(port, sha, token):
    status, body = request(port, "/healthz")
    if status != 200 or json.loads(body).get("commit") != sha:
        raise ReleaseError("REVISION_NOT_LIVE")
    status, body = request(port, "/")
    if status != 401 or b'action="/session"' not in body or b'id="chat-form"' in body:
        raise ReleaseError("LOGIN_PAGE_NOT_LIVE")
    status, body = request(port, "/", token)
    if status != 200 or b'id="chat-form"' not in body:
        raise ReleaseError("AUTHORIZED_UI_NOT_LIVE")
    status, body = request(port, "/drafts", token)
    if status != 200 or "drafts" not in json.loads(body):
        raise ReleaseError("DRAFT_STORE_UNAVAILABLE")
    verify_browser_session(port, token)


def verify_browser_session(port, token):
    """Exercise the actual owner form flow, without printing the key or cookie."""
    login_status, login_body = request(port, "/login")
    if login_status != 200:
        raise ReleaseError("OWNER_LOGIN_FORM_NOT_LIVE")
    nonce = re.search(rb'name="csrf" value="([A-Za-z0-9_-]+)"', login_body)
    if not nonce:
        raise ReleaseError("OWNER_LOGIN_CSRF_NOT_LIVE")
    body = urlencode({"token": token, "csrf": nonce.group(1).decode("ascii")}).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request(
            "POST", "/session", body=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
                # Deliberately test a rewritten proxy Origin.  The one-time
                # form nonce, not a spoofable forwarded Host, must permit it.
                "Origin": "https://reverse-proxy-origin.invalid",
                "Host": "127.0.0.1:" + str(port),
                "Sec-Fetch-Site": "cross-site",
            },
        )
        response = connection.getresponse()
        status = response.status
        cookie = response.getheader("Set-Cookie", "")
        location = response.getheader("Location", "")
        response.read(65536)
    finally:
        connection.close()
    if status != 303 or location != "/" or not cookie.startswith("tg_tumblr_session="):
        raise ReleaseError("OWNER_FORM_SESSION_REJECTED")
    cookie_header = cookie.split(";", 1)[0]
    for path, marker in (("/", b'id="chat-form"'),
                         ("/app.js", b"/assistant"),
                         ("/styles.css", b".draft-")):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            conn.request("GET", path, headers={"Cookie": cookie_header})
            response = conn.getresponse()
            page_status = response.status
            data = response.read(65536)
        finally:
            conn.close()
        if page_status != 200 or marker not in data:
            raise ReleaseError("OWNER_FORM_STATIC_LAYOUT_REJECTED")


def wait_port(port, opened, seconds=12):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        conn = socket.socket()
        conn.settimeout(.5)
        ok = conn.connect_ex(("127.0.0.1", port)) == 0
        conn.close()
        if ok == opened:
            return True
        time.sleep(.3)
    return False


def start(port, release, token, root):
    logdir = root / "dist" / "private-logs"
    logdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    logfile = logdir / ("tumblr-" + str(port) + ".log")
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    env = dict(os.environ)
    env["TUMBLR_APP_ACCESS_TOKEN"] = token
    env["TUMBLR_AI_LIVE_ENABLED"] = "0"
    with os.fdopen(fd, "ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-B", "-m", "tumblrkit.appserve", "serve",
             "--bind", "127.0.0.1", "--port", str(port), "--release", str(release)],
            cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True)
    return process


def find_old(root):
    matches = []
    for item in Path("/proc").iterdir():
        if not item.name.isdigit():
            continue
        try:
            args = [s.decode("utf-8", "replace") for s in
                    (item / "cmdline").read_bytes().split(b"\0") if s]
            if "tumblrkit.appserve" not in args or "serve" not in args:
                continue
            if (item / "cwd").resolve() != root.resolve():
                continue
            matches.append(int(item.name))
        except (OSError, PermissionError, ValueError):
            continue
    if len(matches) != 1:
        raise ReleaseError("OWNER_PROCESS_NOT_UNIQUE")
    return matches[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--target-sha", required=True)
    args = parser.parse_args()
    root = Path.cwd()
    from tumblrkit.appserve import build_release
    token = source_token(args.token_file)
    if git("branch", "--show-current") != TARGET_BRANCH:
        raise ReleaseError("UNEXPECTED_BRANCH")
    if git("status", "--porcelain"):
        raise ReleaseError("DIRTY_CHECKOUT")
    old_pid = find_old(root)
    git("fetch", "--no-tags", "origin", "refs/heads/" + TARGET_BRANCH)
    if git("rev-parse", "FETCH_HEAD") != args.target_sha:
        raise ReleaseError("SOURCE_SHA_CHANGED")
    git("merge", "--ff-only", "FETCH_HEAD")
    if git("rev-parse", "HEAD") != args.target_sha:
        raise ReleaseError("SOURCE_SHA_MISMATCH")
    stage_root = root / "dist-staging"
    stage_release = build_release(root / "app", stage_root, args.target_sha)
    release = root / "dist" / "current"
    if not wait_port(PRODUCTION_PORT, True, 1):
        raise ReleaseError("EXISTING_PRODUCTION_NOT_RUNNING")
    if wait_port(STAGING_PORT, True, 1):
        raise ReleaseError("STAGING_PORT_IN_USE")
    staged = start(STAGING_PORT, stage_release, token, root)
    try:
        if not wait_port(STAGING_PORT, True):
            raise ReleaseError("STAGING_NOT_RUNNING")
        verify(STAGING_PORT, args.target_sha, token)
    finally:
        staged.terminate()
        try:
            staged.wait(timeout=5)
        except subprocess.TimeoutExpired:
            staged.kill()
            staged.wait(timeout=5)
    print("TUMBLR_STAGE=PASS", flush=True)
    # Only after the detached staging app passed all checks, touch the live
    # versioned release symlink. The prior release remains rollback-capable.
    build_release(root / "app", root / "dist", args.target_sha)
    os.kill(old_pid, signal.SIGTERM)
    if not wait_port(PRODUCTION_PORT, False):
        raise ReleaseError("PRODUCTION_PORT_NOT_RELEASED")
    active = start(PRODUCTION_PORT, release, token, root)
    try:
        if not wait_port(PRODUCTION_PORT, True):
            raise ReleaseError("PRODUCTION_NOT_RUNNING")
        verify(PRODUCTION_PORT, args.target_sha, token)
    except Exception:
        if active.poll() is None:
            active.terminate()
            try:
                active.wait(timeout=5)
            except subprocess.TimeoutExpired:
                active.kill()
        from tumblrkit.appserve import rollback_release
        try:
            previous_commit = rollback_release(root / "dist")
            if not wait_port(PRODUCTION_PORT, False, 5):
                raise ReleaseError("ROLLBACK_PORT_BUSY")
            restored = start(PRODUCTION_PORT, root / "dist" / "current", token, root)
            if not wait_port(PRODUCTION_PORT, True):
                raise ReleaseError("ROLLBACK_NOT_RUNNING")
            status, data = request(PRODUCTION_PORT, "/healthz")
            if status != 200 or json.loads(data).get("commit") != previous_commit:
                raise ReleaseError("ROLLBACK_HEALTH_FAILED")
            print("TUMBLR_ROLLBACK=PASS", flush=True)
        except Exception:
            print("TUMBLR_ROLLBACK=FAIL", flush=True)
        raise
    print("TUMBLR_RELEASE=PASS", flush=True)
    print("TUMBLR_REVISION=" + args.target_sha, flush=True)
    print("PROVIDER_API_CALLS=ZERO", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, ValueError, json.JSONDecodeError) as error:
        print("TUMBLR_RELEASE=FAIL", flush=True)
        print("ERROR_CODE=" + str(error).split("\n", 1)[0][:100], flush=True)
        sys.exit(1)
