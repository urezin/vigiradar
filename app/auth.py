"""
Accounts & SSO — dependency-free (standard library + httpx only).

Google / Microsoft OAuth2 authorization-code login with signed-cookie sessions.
Deliberately avoids authlib / python-jose / PyJWT so it never touches
requirements.txt (whose changes were breaking the Render build): tokens are
signed with hmac-SHA256 from the standard library, and the provider token/userinfo
calls go through httpx, which is already a dependency.

Each provider is inert until its client id + secret are configured, so this is
safe to ship before the OAuth apps exist.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from .config import settings

COOKIE_NAME = "vigieye_session"
STATE_COOKIE = "vigieye_oauth_state"

# provider -> endpoints. Microsoft's authorize/token embed the tenant at runtime.
PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "userinfo": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "microsoft": {
        "authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "userinfo": "https://graph.microsoft.com/oidc/userinfo",
        "scope": "openid email profile",
    },
}


# --- signing helpers --------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload: dict) -> str:
    """payload -> '<b64 json>.<b64 hmac>' signed with the session secret."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64e(mac)}"


def _unsign(token: str) -> dict | None:
    """Verify signature + expiry; return payload or None."""
    try:
        body, sig = token.split(".", 1)
        expected = hmac.new(settings.session_secret.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload


# --- sessions ---------------------------------------------------------------

def make_session(user: dict) -> str:
    now = int(time.time())
    return _sign({
        "uid": user["id"],
        "email": user.get("email", ""),
        "name": user.get("name", ""),
        "iat": now,
        "exp": now + settings.session_ttl_days * 86400,
    })


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    return _unsign(token)


# --- oauth flow -------------------------------------------------------------

def provider_ok(provider: str) -> bool:
    if provider == "google":
        return settings.google_enabled
    if provider == "microsoft":
        return settings.microsoft_enabled
    return False


def _client(provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.google_client_id, settings.google_client_secret
    return settings.microsoft_client_id, settings.microsoft_client_secret


def _ep(provider: str, key: str) -> str:
    return PROVIDERS[provider][key].format(tenant=settings.microsoft_tenant)


def redirect_uri(provider: str) -> str:
    return f"{settings.app_base_url.rstrip('/')}/auth/callback/{provider}"


def make_state(provider: str) -> str:
    return _sign({"p": provider, "exp": int(time.time()) + 600})


def check_state(provider: str, state: str) -> bool:
    data = _unsign(state)
    return bool(data and data.get("p") == provider)


def authorize_url(provider: str, state: str) -> str:
    cid, _ = _client(provider)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "scope": PROVIDERS[provider]["scope"],
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"{_ep(provider, 'authorize')}?{urlencode(params)}"


def exchange_code(provider: str, code: str) -> dict:
    import httpx

    cid, secret = _client(provider)
    data = {
        "client_id": cid,
        "client_secret": secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri(provider),
    }
    with httpx.Client(timeout=20) as c:
        r = c.post(_ep(provider, "token"), data=data,
                   headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()


def fetch_userinfo(provider: str, access_token: str) -> dict:
    """Return a normalised {email, name, sub}. Falls back to decoding the
    id_token claims if the userinfo endpoint is unavailable."""
    import httpx

    with httpx.Client(timeout=20) as c:
        r = c.get(_ep(provider, "userinfo"),
                  headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        info = r.json()
    email = info.get("email") or info.get("preferred_username") or ""
    name = info.get("name") or info.get("given_name") or email.split("@")[0]
    sub = info.get("sub") or info.get("oid") or email
    return {"email": email.lower(), "name": name, "sub": str(sub)}


def decode_id_token_claims(id_token: str) -> dict:
    """Best-effort unverified decode of a JWT's claims (email/name). Signature is
    not checked here — we only trust it because it arrived over the direct,
    TLS-protected token exchange with the provider."""
    try:
        _, body, _ = id_token.split(".")
        claims = json.loads(_b64d(body))
    except Exception:
        return {}
    email = (claims.get("email") or claims.get("preferred_username") or "").lower()
    return {"email": email, "name": claims.get("name") or email.split("@")[0],
            "sub": str(claims.get("sub") or claims.get("oid") or email)}
