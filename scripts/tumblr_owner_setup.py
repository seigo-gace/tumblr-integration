"""One-time owner-local interactive setup; never puts the password in GitHub, logs or Chat.

Execution must happen in the owner's interactive WSL shell. The helper is
deliberately not run by Actions, GPT CHAT, a remote public endpoint or CI.
"""
import argparse
import getpass
import hmac
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tumblrkit.appserve import require_token, ServeError


class SetupError(Exception):
    pass


def private_password_file():
    folder = Path.home() / ".config" / "tgtumbler" / "tumblr-operation-app"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.stat().st_mode & 0o077:
        raise SetupError("PRIVATE_FOLDER_PERMISSIONS")
    return folder / "tumblr-app-access-token"


def setup_token(path):
    if path.exists():
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise SetupError("EXISTING_TOKEN_FILE_UNSAFE")
        require_token(path.read_text(encoding="utf-8").strip())
        return "EXISTING"
    value = getpass.getpass("Tumblr操作アプリのログインキー (8文字以上): ")
    require_token(value)
    repeat = getpass.getpass("確認のためもう一度入力: ")
    if not hmac.compare_digest(value.encode("utf-8"), repeat.encode("utf-8")):
        raise SetupError("LOGIN_KEY_CONFIRMATION_MISMATCH")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(value + "\n")
    return "CREATED_BY_OWNER"


def ask_new_token():
    for attempt in range(3):
        value = getpass.getpass("新しいログインキー (8文字以上・空白不可): ")
        try:
            require_token(value)
        except ServeError:
            print("入力形式エラー：8〜256文字、空白・改行なしで入力してください。", flush=True)
            continue
        repeat = getpass.getpass("確認のためもう一度入力: ")
        if not hmac.compare_digest(value.encode("utf-8"), repeat.encode("utf-8")):
            print("確認用の入力が一致しません。", flush=True)
            continue
        return value
    raise SetupError("LOGIN_KEY_INPUT_RETRY_EXCEEDED")


def reset_token(path, root, target_sha):
    # Keep the old on-disk key until the new server has passed its live checks.
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise SetupError("EXISTING_TOKEN_FILE_UNSAFE")
    value = ask_new_token()
    fd, name = tempfile.mkstemp(prefix=".tumblr-login-next-", dir=path.parent)
    pending = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value + "\n")
        del value
        process = subprocess.run(
            [sys.executable, "-B", str(root / "scripts" / "tumblr_release.py"),
             "--token-file", str(pending), "--target-sha", target_sha],
            cwd=root, check=False)
        if process.returncode:
            raise SetupError("RELEASE_FAILED_OLD_KEY_FILE_PRESERVED")
        os.replace(pending, path)
        print("OWNER_TOKEN=UPDATED", flush=True)
    finally:
        if pending.exists():
            pending.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--reset-token", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = private_password_file()
    if args.reset_token:
        reset_token(path, root, args.target_sha)
    else:
        state = setup_token(path)
        print("OWNER_TOKEN=" + state, flush=True)
        process = subprocess.run(
            [sys.executable, "-B", str(root / "scripts" / "tumblr_release.py"),
             "--token-file", str(path), "--target-sha", args.target_sha],
            cwd=root, check=False)
        if process.returncode:
            raise SetupError("RELEASE_FAILED")
    print("TUMBLR_LOGIN_READY=YES", flush=True)
    print("URL=https://tumblr.hentaigay.org/login", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, SetupError) as error:
        print("OWNER_SETUP=FAIL_" + str(error).split("\n", 1)[0][:80], flush=True)
        sys.exit(1)
