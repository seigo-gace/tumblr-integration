#!/usr/bin/env python3
"""One bounded live call per selected provider. Prints status codes only."""
import os
import stat
import sys
import time

from tumblrkit.assistant import ProviderError, providers_from_env

PROMPT = (
    '{"action":"","target":"","reply":"ok","question":""}'
    " というJSONオブジェクトだけを返してください。"
)
SAFE_CODES = {
    "rate_limited",
    "free_tier_exhausted",
    "failed",
    "invalid_response",
    "network_unavailable",
    "ai_disabled",
    "quota_exhausted",
    "quota_cooldown",
    "quota_unavailable",
    "paid_model_forbidden",
}


def run(env, transport=None, sleep=time.sleep):
    mode = env.get("TUMBLR_AI_ROUTING_MODE", "free_router")
    try:
        providers = providers_from_env(env, transport)
    except ProviderError as exc:
        code = exc.code if exc.code in SAFE_CODES else "failed"
        print(f"PROBE=FAIL reason={code}")
        return 2
    names = [item.name for item in providers]
    print(f"ROUTING={mode}")
    print("PROVIDERS=" + ",".join(names))
    if mode == "free_router":
        targets = providers
        if names != ["openrouter"]:
            print("PROBE=FAIL reason=free_router_provider_mismatch")
            return 2
    elif mode == "legacy_split":
        targets = [item for item in providers if item.name in ("gemini", "groq")]
        if [item.name for item in targets] != ["gemini", "groq"]:
            print("PROBE=FAIL reason=legacy_provider_mismatch")
            return 2
    else:
        print("PROBE=FAIL reason=invalid_routing_mode")
        return 2
    if env.get("TUMBLR_AI_LIVE_ENABLED") != "1":
        print("PROBE=FAIL reason=ai_disabled")
        return 2
    path = env.get("TUMBLR_AI_QUOTA_DB", "").strip()
    if not path or not _private_quota(path):
        print("PROBE=FAIL reason=quota_unavailable")
        return 2
    ok = True
    for index, provider in enumerate(targets):
        if index:
            sleep(61)
        try:
            plan = provider.complete(PROMPT)
        except ProviderError as exc:
            code = exc.code if exc.code in SAFE_CODES else "failed"
            print(f"CALL provider={provider.name} result={code}")
            ok = False
            continue
        except Exception:
            print(f"CALL provider={provider.name} result=error")
            ok = False
            continue
        if isinstance(plan, dict):
            print(f"CALL provider={provider.name} result=ok")
        else:
            print(f"CALL provider={provider.name} result=invalid_response")
            ok = False
    print("PROBE=" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def _private_quota(path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        return False
    flags = os.O_CREAT | os.O_RDWR
    if not os.path.exists(path):
        flags |= os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        os.chmod(path, 0o600)
        return stat.S_IMODE(os.stat(path).st_mode) == 0o600
    except OSError:
        return False


def main():
    return run(os.environ)


if __name__ == "__main__":
    sys.exit(main())
