# agentauth

Tiered credential management for agent frameworks. Agents call connections by name — they never see raw tokens.

Tokens live in the OS native keychain (gnome-keyring on Linux, macOS Keychain, Windows Credential Manager). Env var fallback for headless/server deployments.

## Install

```bash
pip install agentauth
pip install secretstorage   # Linux only (gnome-keyring / KWallet)
```

## Quick start

```bash
# Store a token
agentauth add github

# Test it
agentauth test github --path /user

# List all tokens
agentauth list
```

## Config (e.g. BeigeBox config.yaml)

```yaml
connections:
  github:
    type: bearer
    base_url: https://api.github.com
    tier: 1
    allowed_paths:
      - /user/**
      - /repos/**

  openrouter:
    type: bearer
    base_url: https://openrouter.ai/api/v1
    tier: 1
    allowed_paths:
      - /models
      - /chat/**
```

## Use in code

```python
from agentauth import get_registry

registry = get_registry(cfg["connections"])
result = registry.call("github", "GET", "/user")
# {"status": 200, "body": "..."}
```

## Tier system

| Tier | Constant | Meaning |
|------|----------|---------|
| 1 | `TIER_READ` | Agent can call freely |
| 2 | `TIER_WRITE` | Low blast radius writes, reversible |
| 3 | `TIER_SEND` | Sends to external people/systems — require human confirmation |
| 4 | `TIER_NEVER` | Not for agents |

Tier enforcement is the framework's responsibility. `registry.tier(name)` returns the tier so the framework can gate calls appropriately.

## Token storage

**Keychain first, env var fallback.**

```bash
# Store in keychain (recommended)
agentauth add myservice

# Headless/server: set env var
export BB_MYSERVICE_TOKEN=your_token_here
```

## OAuth2 connections

For services that use OAuth (Google, GitHub, Linear, Notion), run a browser consent flow instead of pasting a token:

```bash
agentauth auth google_calendar     # opens browser → consent → stores refresh token
agentauth deauth google_calendar   # remove stored tokens
```

Config:

```yaml
connections:
  google_calendar:
    type: oauth2
    provider: google            # google | github | linear | notion
    client_id: YOUR_CLIENT_ID
    client_secret: YOUR_CLIENT_SECRET
    scopes:
      - https://www.googleapis.com/auth/calendar.readonly
    base_url: https://www.googleapis.com/calendar/v3
    tier: 1
    allowed_paths:
      - /calendars/**
      - /users/**
```

The `client_id` and `client_secret` are your OAuth app credentials (create them in [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials → OAuth 2.0 Client ID, type: Desktop app).

Access tokens are auto-refreshed on expiry — the agent never needs to re-authenticate.

---

## Predefined connection examples

Minimal-scope configs for common services — add the ones you need:

```yaml
connections:
  # GitHub — read-only
  github:
    type: bearer
    base_url: https://api.github.com
    tier: 1
    allowed_paths: ["/user/**", "/repos/**", "/orgs/**"]

  # Linear — read-only
  linear:
    type: bearer
    base_url: https://api.linear.app/graphql
    tier: 1
    allowed_paths: ["/**"]

  # Notion — read-only
  notion:
    type: bearer
    base_url: https://api.notion.com/v1
    tier: 1
    allowed_paths: ["/pages/**", "/databases/**", "/search"]

  # Slack — read-only
  slack:
    type: bearer
    base_url: https://slack.com/api
    tier: 1
    allowed_paths: ["/conversations.list", "/conversations.history", "/users.info"]

  # OpenRouter
  openrouter:
    type: bearer
    base_url: https://openrouter.ai/api/v1
    tier: 1
    allowed_paths: ["/models", "/chat/**"]

  # Google Calendar — read-only (OAuth2)
  google_calendar:
    type: oauth2
    provider: google
    client_id: YOUR_CLIENT_ID
    client_secret: YOUR_CLIENT_SECRET
    scopes: ["https://www.googleapis.com/auth/calendar.readonly"]
    base_url: https://www.googleapis.com/calendar/v3
    tier: 1
    allowed_paths: ["/calendars/**", "/users/**"]

  # Google Gmail — read-only (OAuth2)
  google_gmail:
    type: oauth2
    provider: google
    client_id: YOUR_CLIENT_ID
    client_secret: YOUR_CLIENT_SECRET
    scopes: ["https://www.googleapis.com/auth/gmail.readonly"]
    base_url: https://gmail.googleapis.com/gmail/v1
    tier: 1
    allowed_paths: ["/users/**"]

  # Google Drive — files created by this app only (OAuth2)
  google_drive:
    type: oauth2
    provider: google
    client_id: YOUR_CLIENT_ID
    client_secret: YOUR_CLIENT_SECRET
    scopes: ["https://www.googleapis.com/auth/drive.file"]
    base_url: https://www.googleapis.com/drive/v3
    tier: 2
    allowed_paths: ["/files/**"]
```

## CLI reference

```
agentauth list                          List configured connections + token status
agentauth add <name>                    Prompt for token, store in keychain
agentauth add <name> --env              Read BB_<NAME>_TOKEN from env
agentauth remove <name>                 Delete from keychain
agentauth test <name> [--path /ep]      Test a connection
agentauth auth <name> [--config f]      OAuth2 browser flow (stores refresh token)
agentauth deauth <name>                 Remove stored OAuth2 tokens
agentauth setup                         Print setup instructions
```

## License

MIT
