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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tumblrkit.appserve import require_token


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
    value = getpass.getpass("Tumblr操作アプリのログインキー (英数字16文字以上): ")
    require_token(value)
    repeat = getpass.getpass("確認のためもう一度入力: ")
    if not hmac.compare_digest(value, repeat):
        raise SetupError("LOGIN_KEY_CONFIRMATION_MISMATCH")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(value + "\n")
    return "CREATED_BY_OWNER"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-sha", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = private_password_file()
    state = setup_token(path)
    print("OWNER_TOKEN=" + state, flush=True)
    process = subprocess.run(
        [sys.executable, "-B", str(root / "scripts" / "tumblr_release.py"),
         "--token-file", str(path), "--target-sha", args.target_sha],
        cwd=root, check=False)
    if process.returncode:
        raise SetupError("RELEASE_FAILED")
    print("TUMBLR_LOGIN_READY=YES", flush=True)
    print("URL=https://tumblr.hentaigay.org/", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, SetupError) as error:
        print("OWNER_SETUP=FAIL_" + str(error).split("\n", 1)[0][:80], flush=True)
        sys.exit(1)
