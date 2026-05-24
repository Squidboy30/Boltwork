mcp-name: io.github.Squidboy30/boltwork-mcp
# boltwork-mcp
**MCP server for Boltwork — AI services that pay for themselves via Bitcoin Lightning.**

Give your AI agent PDF summarisation, code review, translation, web extraction, document comparison, and persistent memory — all paid autonomously in sats. No API keys. No subscriptions. No accounts.

[![PyPI](https://img.shields.io/pypi/v/boltwork-mcp)](https://pypi.org/project/boltwork-mcp/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![API](https://img.shields.io/badge/API-parsebit.fly.dev-green)](https://parsebit.fly.dev)

---

## What this is

[Boltwork](https://parsebit.fly.dev) is a pay-per-call AI services API that uses the [L402 protocol](https://github.com/lightninglabs/L402) — your agent makes a request, receives a Lightning invoice, pays it automatically, and gets the result back. No human involved.

This package wraps Boltwork as an [MCP server](https://modelcontextprotocol.io) so any MCP-compatible AI (Claude, Cursor, Windsurf, etc.) can use it as a tool — with payments handled transparently in the background.

---

## Try it immediately — no wallet required

Two tools work right now with zero setup:

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {}
    }
  }
}
```

Then ask your AI:
```
"Use trial_review_code to review this: def add(a, b): return a + b"
"Use trial_summarise to summarise this: <paste any text>"
```

Real AI results instantly. No Lightning wallet. No setup. Rate limited to 5 calls/hour.

---

## Install

```bash
pip install boltwork-mcp

# If using NWC (Alby, Mutiny, Coinos, etc.):
pip install "boltwork-mcp[nwc]"
```

Or use directly with `uvx` — no install needed:

```bash
uvx boltwork-mcp
```

---

## Setup — pick a wallet

Four wallet backends are supported. Pick whichever fits your setup:

### Option A — NWC / Nostr Wallet Connect *(easiest)*
Works with **Alby**, **Mutiny Wallet**, **Coinos**, **Primal**, **Cashu.me**, and any NWC-compatible wallet.

1. Get a connection string:
   - **Alby** — go to [nwc.getalby.com](https://nwc.getalby.com), create a budget, copy the string
   - **Mutiny** — Settings → Connections → Add connection
   - **Coinos** — [coinos.io](https://coinos.io) → Settings → Nostr Wallet Connect
2. Add to your MCP config:

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "NWC_CONNECTION_STRING": "nostr+walletconnect://your-string-here"
      }
    }
  }
}
```

Requires: `pip install "boltwork-mcp[nwc]"`

---

### Option B — LNbits
Works with **lnbits.com** or any self-hosted LNbits instance. Popular with BTCPay Server users and home node operators.

1. Create a wallet at [lnbits.com](https://lnbits.com) or your instance
2. Go to API info → copy your Invoice/read key

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "LNBITS_URL": "https://lnbits.com",
        "LNBITS_API_KEY": "your-invoice-key-here"
      }
    }
  }
}
```

For self-hosted: set `LNBITS_URL` to your instance URL (e.g. `https://lnbits.yourdomain.com`).

---

### Option C — Strike
Works with a **Strike** account. Custodial, simple API key setup. Good for US users or anyone who already uses Strike.

1. Create an account at [strike.me](https://strike.me)
2. Go to [dashboard.strike.me/developers/api-keys](https://dashboard.strike.me/developers/api-keys) → create an API key

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "STRIKE_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

---

### Option D — Phoenixd
Works with **Phoenixd** — ACINQ's self-hosted Lightning node. Simple REST API, no channel management.

1. Install Phoenixd: [phoenix.acinq.co/server](https://phoenix.acinq.co/server)
2. Get your HTTP password from the Phoenixd config

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "PHOENIXD_URL": "http://localhost:9740",
        "PHOENIXD_PASSWORD": "your-password-here"
      }
    }
  }
}
```

---

## MCP config locations

| Client | Config file |
|--------|------------|
| Claude Desktop (macOS) | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Claude Desktop (Windows) | `%APPDATA%\Claude\claude_desktop_config.json` |
| Cursor | `.cursor/mcp.json` in your project |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` |

---

## Available tools

| Tool | What it does | Cost |
|------|-------------|------|
| `trial_summarise` | Summarise text — free trial | Free |
| `trial_review_code` | Review code — free trial | Free |
| `summarise_pdf` | Summarise a PDF from URL | 500 sats |
| `summarise_webpage` | Summarise any web page | 100 sats |
| `review_code` | Full code review with bugs, security, quality | 2000 sats |
| `review_code_url` | Review code from GitHub/GitLab URL | 2000 sats |
| `extract_data` | Extract structured data from PDF | 200 sats |
| `translate` | Translate text or document (24 languages) | 150 sats |
| `extract_tables` | Extract all tables from a PDF | 300 sats |
| `compare_documents` | Diff two PDFs | 500 sats |
| `explain_code` | Explain code in plain English | 500 sats |
| `memory_store` | Store persistent agent memory | 10 sats |
| `memory_retrieve` | Retrieve agent memory | 5 sats |
| `memory_delete` | Delete a memory key | Free |
| `run_workflow` | Chain multiple services in one call | 1000 sats |

---

## Payment flow

When your agent calls a paid tool:

1. boltwork-mcp calls the Boltwork API
2. Receives HTTP 402 with a Lightning invoice
3. Pays the invoice automatically using your configured wallet
4. Retries the request with the payment proof
5. Returns the result to your agent

Your agent never sees this — it just gets the result.

---

## Links

- [Boltwork API](https://parsebit.fly.dev) — live API
- [Agent spec](https://parsebit.fly.dev/agent-spec.md) — full endpoint documentation
- [L402 manifest](https://parsebit.fly.dev/.well-known/l402.json) — machine-readable service discovery
- [Cracked Minds](https://crackedminds.co.uk) — by Cracked Minds
