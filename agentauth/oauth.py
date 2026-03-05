"""
agentauth OAuth2 — browser authorization code flow + auto-refresh.

Supports Google OAuth2 out of the box. Generic OAuth2 provider config works
for anything else that follows the standard authorization_code flow.

Token storage:
  Stored in OS keychain under the key:  oauth__{name}
  Value: JSON {"access_token": ..., "refresh_token": ..., "expires_at": ...}

Usage:
  agentauth auth google_calendar     # opens browser, stores tokens
  agentauth deauth google_calendar   # removes stored tokens

Config (config.yaml):
  connections:
    google_calendar:
      type: oauth2
      provider: google
      client_id: YOUR_CLIENT_ID
      client_secret: YOUR_CLIENT_SECRET
      scopes:
        - https://www.googleapis.com/auth/calendar.readonly
      base_url: https://www.googleapis.com/calendar/v3
      tier: 1
      allowed_paths:
        - /calendars/**
        - /users/**
"""
from __future__ import annotations

import json
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .registry import KEYRING_SERVICE

_OAUTH_PREFIX = "oauth__"

# ─────────────────────────────────────────────────────────────────────────────
# Provider definitions
# ─────────────────────────────────────────────────────────────────────────────

PROVIDERS: dict[str, dict] = {
    "google": {
        "auth_url":  "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    },
    "github": {
        "auth_url":  "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
    },
    "linear": {
        "auth_url":  "https://linear.app/oauth/authorize",
        "token_url": "https://api.linear.app/oauth/token",
    },
    "notion": {
        "auth_url":  "https://api.notion.com/v1/oauth/authorize",
        "token_url": "https://api.notion.com/v1/oauth/token",
    },
}

# Predefined minimum-scope configs — reference for config.yaml
PREDEFINED: dict[str, dict] = {
    "google_calendar": {
        "provider": "google",
        "scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
        "base_url": "https://www.googleapis.com/calendar/v3",
        "tier": 1,
        "allowed_paths": ["/calendars/**", "/users/**"],
    },
    "google_gmail": {
        "provider": "google",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
        "base_url": "https://gmail.googleapis.com/gmail/v1",
        "tier": 1,
        "allowed_paths": ["/users/**"],
    },
    "google_drive": {
        "provider": "google",
        # drive.file = only files this app created/opened, not all Drive
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
        "base_url": "https://www.googleapis.com/drive/v3",
        "tier": 2,
        "allowed_paths": ["/files/**"],
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Keychain helpers for OAuth tokens
# ─────────────────────────────────────────────────────────────────────────────

def _store_tokens(name: str, tokens: dict) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, f"{_OAUTH_PREFIX}{name}", json.dumps(tokens))


def _load_tokens(name: str) -> dict | None:
    try:
        import keyring
        raw = keyring.get_password(KEYRING_SERVICE, f"{_OAUTH_PREFIX}{name}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


def _delete_tokens(name: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, f"{_OAUTH_PREFIX}{name}")
    except Exception:
        pass


def oauth_token_source(name: str) -> str:
    tokens = _load_tokens(name)
    if not tokens:
        return "MISSING"
    if tokens.get("refresh_token"):
        return "keychain (oauth)"
    return "keychain (oauth — no refresh)"


# ─────────────────────────────────────────────────────────────────────────────
# Access token resolution with auto-refresh
# ─────────────────────────────────────────────────────────────────────────────

def get_access_token(name: str, conn_cfg: dict) -> str:
    """
    Return a valid access token, refreshing automatically if expired.
    Raises RuntimeError if no tokens are stored or refresh fails.
    """
    tokens = _load_tokens(name)
    if not tokens:
        raise RuntimeError(
            f"No OAuth tokens for '{name}'. Run: agentauth auth {name}"
        )

    # Still valid — return immediately
    if time.time() < tokens.get("expires_at", 0) - 60:
        return tokens["access_token"]

    # Expired — refresh
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(
            f"No refresh token for '{name}'. Re-authenticate: agentauth auth {name}"
        )

    provider = conn_cfg.get("provider", "google")
    token_url = (
        PROVIDERS.get(provider, {}).get("token_url")
        or conn_cfg.get("token_url")
    )

    resp = httpx.post(token_url, data={
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     conn_cfg.get("client_id"),
        "client_secret": conn_cfg.get("client_secret"),
    })
    resp.raise_for_status()
    new_tokens = resp.json()

    # Google doesn't always return a new refresh_token — preserve the old one
    if "refresh_token" not in new_tokens:
        new_tokens["refresh_token"] = refresh_token
    new_tokens["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)

    _store_tokens(name, new_tokens)
    return new_tokens["access_token"]


# ─────────────────────────────────────────────────────────────────────────────
# Full browser OAuth flow
# ─────────────────────────────────────────────────────────────────────────────

def do_auth_flow(name: str, conn_cfg: dict) -> None:
    """
    Run the browser-based OAuth2 authorization code flow.
    - Opens browser to provider consent screen
    - Spins up a local HTTP server to catch the redirect
    - Exchanges code for tokens
    - Stores refresh_token (and access_token) in OS keychain
    """
    provider = conn_cfg.get("provider", "google")
    prov_cfg  = PROVIDERS.get(provider, {})

    auth_url_base = prov_cfg.get("auth_url") or conn_cfg.get("auth_url")
    token_url     = prov_cfg.get("token_url") or conn_cfg.get("token_url")
    client_id     = conn_cfg.get("client_id")
    client_secret = conn_cfg.get("client_secret")
    scopes        = conn_cfg.get("scopes", [])

    if not auth_url_base:
        raise ValueError(f"No auth_url for provider '{provider}'")
    if not token_url:
        raise ValueError(f"No token_url for provider '{provider}'")
    if not client_id:
        raise ValueError(f"Missing client_id for '{name}' in config")
    if not client_secret:
        raise ValueError(f"Missing client_secret for '{name}' in config")

    port         = _free_port()
    redirect_uri = f"http://localhost:{port}/callback"
    code_holder: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)
            if "code" in qs:
                code_holder["code"] = qs["code"][0]
                body = b"<h1>Authorized. You can close this tab.</h1>"
                self.send_response(200)
            else:
                code_holder["error"] = qs.get("error", ["unknown"])[0]
                body = f"<h1>Error: {code_holder['error']}</h1>".encode()
                self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = HTTPServer(("localhost", port), _Handler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    params = {
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         " ".join(scopes),
        "access_type":   "offline",   # request refresh_token
        "prompt":        "consent",   # always show consent (ensures refresh_token)
    }
    full_auth_url = f"{auth_url_base}?{urlencode(params)}"

    print(f"Opening browser for '{name}' authorization...")
    print(f"If it doesn't open automatically, visit:\n  {full_auth_url}\n")
    webbrowser.open(full_auth_url)

    thread.join(timeout=120)
    server.server_close()

    if "error" in code_holder:
        raise RuntimeError(f"OAuth error: {code_holder['error']}")
    if "code" not in code_holder:
        raise RuntimeError("Timed out waiting for OAuth callback (120s)")

    # Exchange authorization code for tokens
    resp = httpx.post(token_url, data={
        "grant_type":   "authorization_code",
        "code":         code_holder["code"],
        "redirect_uri": redirect_uri,
        "client_id":    client_id,
        "client_secret": client_secret,
    }, headers={"Accept": "application/json"})
    resp.raise_for_status()

    tokens = resp.json()
    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    _store_tokens(name, tokens)
    print(f"Authorized. Tokens stored in OS keychain for '{name}'.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]
