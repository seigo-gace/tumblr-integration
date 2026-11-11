"""Tumblr OAuth2 authorization-code adapter. Network and secret persistence are caller-owned."""
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse, parse_qs
import secrets

AUTHORIZE = "https://www.tumblr.com/oauth2/authorize"
TOKEN = "https://api.tumblr.com/v2/oauth2/token"
USER_INFO = "https://api.tumblr.com/v2/user/info"
SCOPES = frozenset({"basic", "write", "offline_access"})

class Transport(Protocol):
    def post_form(self, url: str, data: dict[str, str]) -> dict[str, Any]: ...
    def get_json(self, url: str, access_token: str) -> dict[str, Any]: ...

@dataclass(frozen=True)
class AuthorizationStart:
    url: str
    state: str

@dataclass(frozen=True)
class AccountTokens:
    access_token: str
    token_type: str
    refresh_token: str | None
    expires_in: int | None
    scopes: tuple[str, ...] = ()

class TumblrOAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, transport: Transport):
        if not client_id or not client_secret:
            raise ValueError("OAuth client configuration missing")
        parsed = urlparse(redirect_uri)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("HTTPS redirect URI required")
        self._client_id, self._secret = client_id, client_secret
        self._redirect_uri, self._transport = redirect_uri, transport

    def begin(self, scopes: tuple[str, ...] = ("basic",)) -> AuthorizationStart:
        if not scopes or not set(scopes).issubset(SCOPES) or len(set(scopes)) != len(scopes):
            raise ValueError("unsupported or duplicate OAuth scope")
        state = secrets.token_urlsafe(32)
        args = {"client_id": self._client_id, "response_type": "code", "scope": " ".join(scopes),
                "state": state, "redirect_uri": self._redirect_uri}
        return AuthorizationStart(AUTHORIZE + "?" + urlencode(args), state)

    def exchange(self, callback_query: str, expected_state: str) -> AccountTokens:
        params = parse_qs(callback_query.removeprefix("?"), keep_blank_values=True)
        if any(len(v) != 1 for v in params.values()):
            raise ValueError("duplicate callback parameter")
        state = params.get("state", [""])[0]
        if not expected_state or not secrets.compare_digest(state, expected_state):
            raise PermissionError("OAuth state mismatch")
        if "error" in params:
            raise PermissionError("OAuth authorization rejected")
        code = params.get("code", [""])[0]
        if not code:
            raise ValueError("OAuth authorization code missing")
        response = self._transport.post_form(TOKEN, {"grant_type": "authorization_code", "code": code,
            "client_id": self._client_id, "client_secret": self._secret, "redirect_uri": self._redirect_uri})
        token = response.get("access_token") if isinstance(response, dict) else None
        kind = response.get("token_type") if isinstance(response, dict) else None
        if not isinstance(token, str) or not token or not isinstance(kind, str) or kind.lower() != "bearer":
            raise ValueError("invalid OAuth token response")
        refresh = response.get("refresh_token")
        expiry = response.get("expires_in")
        if refresh is not None and (not isinstance(refresh, str) or not refresh):
            raise ValueError("invalid refresh token")
        if expiry is not None and (type(expiry) is not int or expiry <= 0):
            raise ValueError("invalid token lifetime")
        # OAuth servers may return a space-delimited scope string. Unknown or
        # narrowed scopes must not authorize publishing.
        scopes=response.get("scope")
        if scopes is None:
            granted=()
        elif isinstance(scopes,str) and set(scopes.split()).issubset(SCOPES):
            granted=tuple(scopes.split())
        else:
            raise ValueError("oauth_scopes_invalid")
        return AccountTokens(token, kind, refresh, expiry, granted)

    def verify_identity(self, tokens: AccountTokens) -> str:
        result = self._transport.get_json(USER_INFO, tokens.access_token)
        user = result.get("response", {}).get("user") if isinstance(result, dict) else None
        name = user.get("name") if isinstance(user, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError("Tumblr identity readback missing")
        return name

def read_owner_client_credentials(directory):
    """Load the two previously owner-registered OAuth app credentials, never log values.

    Does not authorize a Tumblr account or issue an access token.
    """
    import os
    import stat
    from pathlib import Path

    folder = Path(directory).expanduser()
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("oauth_credentials_directory_unavailable")
    folder_stat = folder.stat()
    if folder_stat.st_uid != os.getuid() or folder_stat.st_mode & 0o077:
        raise ValueError("oauth_credentials_directory_unsafe")
    values = []
    for name in ("consumer-key", "client-secret"):
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("oauth_credential_file_unavailable")
        file_stat = path.stat()
        if (not stat.S_ISREG(file_stat.st_mode) or
                file_stat.st_uid != os.getuid() or file_stat.st_mode & 0o077 or
                not 12 <= file_stat.st_size <= 4097):
            raise ValueError("oauth_credential_file_unsafe")
        value = path.read_text(encoding="utf-8").strip()
        if not 12 <= len(value) <= 4096 or any(ch.isspace() for ch in value):
            raise ValueError("oauth_credential_invalid")
        values.append(value)
    return tuple(values)
