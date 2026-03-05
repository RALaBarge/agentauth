"""
AgentAuth — connection registry and credentialed HTTP dispatch.

Tokens stored in OS native keychain (gnome-keyring / macOS Keychain /
Windows Credential Manager) via python-keyring. Env var fallback for
headless/server deployments: BB_<NAME>_TOKEN.

The caller never sees a raw token — it calls a connection by name,
the registry injects Authorization headers internally, and only the
response body is returned.
"""
from __future__ import annotations

import fnmatch
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "agentauth"

# Tier definitions — enforced by the caller (agent framework), not here.
# Registry exposes the tier so the framework can decide what confirmation
# to require before executing.
TIER_READ    = 1   # safe to call freely
TIER_WRITE   = 2   # low blast radius writes, reversible
TIER_SEND    = 3   # sends to other people / external systems — require human confirmation
TIER_NEVER   = 4   # not for agents — defined here only to document the boundary


# ─────────────────────────────────────────────────────────────────────────────
# Keychain helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_token(name: str) -> str | None:
    """Resolve token: keychain first, env var fallback."""
    try:
        import keyring
        token = keyring.get_password(KEYRING_SERVICE, name)
        if token:
            return token
    except Exception as e:
        logger.debug("keyring unavailable for '%s': %s", name, e)

    return os.environ.get(f"BB_{name.upper()}_TOKEN")


def set_token(name: str, token: str) -> None:
    """Store token in OS keychain."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, name, token)
        logger.info("Stored token for '%s' in OS keychain", name)
    except Exception as e:
        raise RuntimeError(
            f"Could not store token in OS keychain: {e}\n"
            f"Linux: pip install secretstorage\n"
            f"Headless fallback: set BB_{name.upper()}_TOKEN env var"
        ) from e


def delete_token(name: str) -> None:
    """Remove token from OS keychain."""
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, name)
    except Exception as e:
        raise RuntimeError(f"Could not remove token from keychain: {e}") from e


def token_source(name: str) -> str:
    """Return human-readable description of where the token comes from."""
    try:
        import keyring
        if keyring.get_password(KEYRING_SERVICE, name):
            return "keychain"
    except Exception:
        pass
    if os.environ.get(f"BB_{name.upper()}_TOKEN"):
        return "env var"
    return "MISSING"


# ─────────────────────────────────────────────────────────────────────────────
# Connection registry
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionRegistry:
    """
    Resolves named connections from a config dict and makes credentialed
    HTTP calls. Config is metadata only — no secrets.

    Config structure:
      {
        "github": {
          "type": "bearer",
          "base_url": "https://api.github.com",
          "tier": 1,
          "allowed_paths": ["/user/**", "/repos/**"]
        }
      }
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg

    def list(self) -> list[dict]:
        """Return list of connection info dicts."""
        from .oauth import oauth_token_source
        results = []
        for name, conn in sorted(self._cfg.items()):
            if conn.get("type") == "oauth2":
                source = oauth_token_source(name)
            else:
                source = token_source(name)
            results.append({
                "name":   name,
                "type":   conn.get("type", "bearer"),
                "tier":   conn.get("tier", TIER_READ),
                "source": source,
            })
        return results

    def tier(self, name: str) -> int:
        """Return the tier for a named connection."""
        conn = self._cfg.get(name)
        if conn is None:
            raise ValueError(f"Unknown connection: {name}")
        return conn.get("tier", TIER_READ)

    def call(
        self,
        name: str,
        method: str,
        path: str,
        body: Any = None,
        headers: dict | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """
        Make a credentialed HTTP call. Returns {"status": int, "body": str}.

        NOTE: Does NOT enforce tier confirmation — that is the caller's
        responsibility. Check self.tier(name) before calling write endpoints.
        """
        conn_cfg = self._cfg.get(name)
        if conn_cfg is None:
            raise ValueError(
                f"Unknown connection '{name}'. "
                f"Known: {sorted(self._cfg.keys())}"
            )

        # Path allowlist
        allowed = conn_cfg.get("allowed_paths", ["/**"])
        if not any(fnmatch.fnmatch(path, pat) for pat in allowed):
            raise PermissionError(
                f"Path '{path}' not in allowlist for '{name}'. "
                f"Allowed: {allowed}"
            )

        # Resolve token — oauth2 connections auto-refresh; bearer connections use keychain/env
        conn_type = conn_cfg.get("type", "bearer")
        if conn_type == "oauth2":
            from .oauth import get_access_token
            token = get_access_token(name, conn_cfg)
        else:
            token = get_token(name)

        if not token:
            raise RuntimeError(
                f"No token for '{name}'. "
                f"Run: agentauth add {name}"
            )

        base_url    = conn_cfg.get("base_url", "").rstrip("/")
        url         = base_url + path
        req_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            req_headers.update(headers)

        logger.info("agentauth.call: %s %s %s", name, method.upper(), path)

        with httpx.Client(timeout=timeout) as client:
            resp = client.request(method.upper(), url, headers=req_headers, json=body)

        logger.info(
            "agentauth.call response: %s %s → %d (%d bytes)",
            name, path, resp.status_code, len(resp.content)
        )

        return {"status": resp.status_code, "body": resp.text}


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────

_registry: ConnectionRegistry | None = None


def get_registry(cfg: dict) -> ConnectionRegistry:
    global _registry
    if _registry is None:
        _registry = ConnectionRegistry(cfg)
    return _registry
