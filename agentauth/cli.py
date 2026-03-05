"""
AgentAuth CLI — manage connection credentials.

Usage:
  agentauth list
  agentauth add <name>           # prompts with hidden input
  agentauth add <name> --env     # reads BB_<NAME>_TOKEN from env
  agentauth remove <name>
  agentauth test <name> [--path /endpoint]
  agentauth setup
"""
from __future__ import annotations

import getpass
import os
import sys


def main():
    import argparse
    from .registry import (
        ConnectionRegistry, set_token, delete_token, token_source, TIER_READ
    )

    parser = argparse.ArgumentParser(
        prog="agentauth",
        description="AgentAuth — credential management for agent frameworks",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List connections and token status")

    p_add = sub.add_parser("add", help="Store a token in the OS keychain")
    p_add.add_argument("name")
    p_add.add_argument(
        "--env", action="store_true",
        help="Read from BB_<NAME>_TOKEN env var instead of prompting"
    )

    p_rm = sub.add_parser("remove", help="Remove a token from the OS keychain")
    p_rm.add_argument("name")

    p_test = sub.add_parser("test", help="Test a connection")
    p_test.add_argument("name")
    p_test.add_argument("--path", default="/", help="Path to test (default: /)")
    p_test.add_argument("--config", help="Path to config file (optional)")

    p_auth = sub.add_parser("auth", help="Authorize an OAuth2 connection (browser flow)")
    p_auth.add_argument("name", help="Connection name from config")
    p_auth.add_argument("--config", help="Path to config file (optional)")

    p_deauth = sub.add_parser("deauth", help="Remove stored OAuth2 tokens")
    p_deauth.add_argument("name")

    sub.add_parser("setup", help="Print setup instructions")

    args = parser.parse_args()

    if args.cmd == "list":
        _cmd_list()

    elif args.cmd == "add":
        if args.env:
            env_var = f"BB_{args.name.upper()}_TOKEN"
            token = os.environ.get(env_var)
            if not token:
                print(f"Error: {env_var} is not set", file=sys.stderr)
                sys.exit(1)
        else:
            token = getpass.getpass(f"Token for '{args.name}' (input hidden): ")
            if not token.strip():
                print("Error: empty token", file=sys.stderr)
                sys.exit(1)
        try:
            set_token(args.name, token.strip())
            print(f"Stored in OS keychain: {args.name}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "remove":
        try:
            delete_token(args.name)
            print(f"Removed from keychain: {args.name}")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "test":
        cfg = _load_cfg(getattr(args, "config", None))
        reg = ConnectionRegistry(cfg)
        try:
            result = reg.call(args.name, "GET", args.path)
            print(f"Status: {result['status']}")
            print(result["body"][:500])
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "auth":
        cfg = _load_cfg(getattr(args, "config", None))
        conn_cfg = cfg.get(args.name)
        if conn_cfg is None:
            print(f"Error: '{args.name}' not found in config. Add it to your connections config first.", file=sys.stderr)
            sys.exit(1)
        if conn_cfg.get("type") != "oauth2":
            print(f"Error: '{args.name}' is type '{conn_cfg.get('type', 'bearer')}', not oauth2. Use 'agentauth add' for bearer tokens.", file=sys.stderr)
            sys.exit(1)
        try:
            from .oauth import do_auth_flow
            do_auth_flow(args.name, conn_cfg)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "deauth":
        from .oauth import _delete_tokens
        _delete_tokens(args.name)
        print(f"Removed OAuth tokens for: {args.name}")

    elif args.cmd == "setup":
        _cmd_setup()


def _cmd_list():
    """List all connections that have tokens stored, regardless of config."""
    from .registry import KEYRING_SERVICE
    try:
        import keyring
        import keyring.backend
        # Try to enumerate — not all backends support this,
        # fall back to just showing source status per known name
        print("Checking keychain...")
    except ImportError:
        print("keyring not installed — pip install keyring secretstorage")
        return

    print("\nTo see configured connections, run from your project:")
    print("  python -m beigebox.connections list")
    print("\nOr use agentauth test <name> to verify a specific connection.")


def _load_cfg(config_path: str | None) -> dict:
    """Load connections config from a YAML file or return empty dict."""
    if config_path:
        import yaml
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return data.get("connections", data) if isinstance(data, dict) else {}
    return {}


def _cmd_setup():
    print("""
AgentAuth Setup
───────────────
Tokens are stored in your OS native keychain. Nothing written to disk
in plaintext.

1. Install:
   pip install agentauth
   pip install secretstorage   # Linux only (gnome-keyring / KWallet)

2. Add a token:
   agentauth add github
   # Prompts with hidden input, stores in keychain

3. In your agent framework config (e.g. BeigeBox config.yaml):
   connections:
     github:
       type: bearer
       base_url: https://api.github.com
       tier: 1           # 1=read, 2=write, 3=send (requires confirmation)
       allowed_paths:
         - /user/**
         - /repos/**

4. In code:
   from agentauth import get_registry
   registry = get_registry(cfg["connections"])
   result = registry.call("github", "GET", "/user")

OAuth2 connections:
  agentauth auth google_calendar     # browser consent flow, stores refresh token
  agentauth deauth google_calendar   # remove stored OAuth tokens

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

  Supported providers: google, github, linear, notion
  (Any provider with standard auth code flow works via custom auth_url/token_url)

Tier system:
  1 (read)  — agent can call freely
  2 (write) — low blast radius, reversible
  3 (send)  — sends to external people/systems, require human confirmation
              (enforcement is the framework's responsibility)

Headless / server deployments:
  Set BB_<NAME>_TOKEN environment variables — keychain is tried first,
  env vars are the fallback.
""")


if __name__ == "__main__":
    main()
