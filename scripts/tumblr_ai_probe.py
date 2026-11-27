#!/usr/bin/env python3
"""One bounded live call per selected provider. Prints status codes only."""
import os
import re
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
    "model_forbidden",
    "failed",
    "invalid_response",
    "network_unavailable",
    "ai_disabled",
    "quota_exhausted",
    "quota_cooldown",
    "quota_unavailable",
    "paid_model_forbidden",
}
LAST_STATUS = {"value": None, "detail": ""}
_DETAIL = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def run(env, transport=None, sleep=time.sleep):
    mode = env.get("TUMBLR_AI_ROUTING_MODE", "free_router")
    selected = _StatusTransport(transport) if transport is not None else None
    try:
        providers = providers_from_env(env, selected)
    except ProviderError as exc:
        code = exc.code if exc.code in SAFE_CODES else "failed"
        print(f"PROBE=FAIL reason={code}")
        return 2
    names = [item.name for item in providers]
    print(f"ROUTING={mode}")
    print("PROVIDERS=" + ",".join(names))
    only = env.get("TUMBLR_AI_PROBE_ONLY", "").strip()
    if mode == "free_router":
        targets = providers
        if names != ["openrouter"]:
            print("PROBE=FAIL reason=free_router_provider_mismatch")
            return 2
    elif mode == "legacy_split":
        targets = [item for item in providers if item.name in ("gemini", "groq")]
        if not only and [item.name for item in targets] != ["gemini", "groq"]:
            print("PROBE=FAIL reason=legacy_provider_mismatch")
            return 2
    else:
        print("PROBE=FAIL reason=invalid_routing_mode")
        return 2
    if only:
        targets = [item for item in targets if item.name == only]
        if [item.name for item in targets] != [only]:
            print("PROBE=FAIL reason=probe_provider_mismatch")
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
        LAST_STATUS["value"] = None
        LAST_STATUS["detail"] = ""
        try:
            plan = provider.complete(PROMPT)
        except ProviderError as exc:
            code = exc.code if exc.code in SAFE_CODES else "failed"
            print(f"CALL provider={provider.name} result={code}{_http()}{_detail()}")
            ok = False
            continue
        except Exception:
            print(f"CALL provider={provider.name} result=error")
            ok = False
            continue
        if isinstance(plan, dict):
            print(f"CALL provider={provider.name} result=ok")
        else:
            print(f"CALL provider={provider.name} result=invalid_response{_http()}")
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


class _StatusTransport:
    def __init__(self, inner):
        self.inner = inner

    def post(self, url, headers, body):
        status, payload = self.inner.post(url, headers, body)
        LAST_STATUS["value"] = status
        LAST_STATUS["detail"] = _safe_detail(payload)
        return status, payload


def _http():
    status = LAST_STATUS["value"]
    if isinstance(status, int) and 100 <= status <= 599:
        return f" http={status}"
    return ""


def _safe_detail(payload):
    if not isinstance(payload, dict) or not payload:
        return "empty"
    error = payload.get("error")
    if error == "quota":
        return "quota"
    if not isinstance(error, dict):
        return "unclassified"
    parts = []
    for key in ("status", "type", "code"):
        value = error.get(key)
        if isinstance(value, str) and _DETAIL.fullmatch(value):
            parts.append(value)
    return ",".join(parts) or "unclassified"


def _detail():
    detail = LAST_STATUS["detail"]
    if detail and _DETAIL.fullmatch(detail.replace(",", "")):
        return " detail=" + detail
    return ""


def main():
    import tumblrkit.assistant as assistant_mod
    from tumblrkit.assistant import UrllibTransport

    class RecordingTransport(UrllibTransport):
        def post(self, url, headers, body):
            status, payload = super().post(url, headers, body)
            LAST_STATUS["value"] = status
            LAST_STATUS["detail"] = _safe_detail(payload)
            return status, payload

    assistant_mod.UrllibTransport = RecordingTransport
    return run(os.environ)


if __name__ == "__main__":
    sys.exit(main())
