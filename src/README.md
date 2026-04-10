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

## Install

```bash
pip install boltwork-mcp

# If using NWC (Nostr Wallet Connect):
pip install "boltwork-mcp[nwc]"
```

Or use directly with `uvx` — no install needed:

```bash
uvx boltwork-mcp
```

## Setup

### 1. Get a Lightning wallet

You need a Lightning wallet that supports either:

**Option A — NWC (recommended, easiest)**
- [Alby](https://getalby.com) — browser extension, free, gives you an NWC connection string
- [Mutiny Wallet](https://mutinywallet.com) — self-custodial mobile wallet

**Option B — Phoenixd**
- [Phoenixd](https://phoenix.acinq.co/server) — self-hosted Lightning node, simple REST API

### 2. Add to your MCP config

**Claude Desktop** — edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "NWC_CONNECTION_STRING": "nostr+walletconnect://your-connection-string-here"
      }
    }
  }
}
```

**Cursor** — edit `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "NWC_CONNECTION_STRING": "nostr+walletconnect://your-connection-string-here"
      }
    }
  }
}
```

**Using Phoenixd instead of NWC:**

```json
{
  "mcpServers": {
    "boltwork": {
      "command": "uvx",
      "args": ["boltwork-mcp"],
      "env": {
        "PHOENIXD_URL": "http://localhost:9740",
        "PHOENIXD_PASSWORD": "your-phoenixd-password"
      }
    }
  }
}
```

### 3. Restart your AI and start using tools

That's it. Your agent now has access to all Boltwork tools and will pay invoices automatically when it uses them.

---

## Available tools

| Tool | What it does | Cost |
|------|-------------|------|
| `summarise_pdf` | Summarise a PDF from URL | 500 sats |
| `review_code` | Review code for bugs, security, quality | 2000 sats |
| `review_code_url` | Review code from GitHub/GitLab URL | 2000 sats |
| `summarise_webpage` | Summarise any web page | 100 sats |
| `extract_data` | Extract structured data from PDF | 200 sats |
| `translate` | Translate text or document to 24 languages | 150 sats |
| `extract_tables` | Extract tables from PDF as structured JSON | 300 sats |
| `compare_documents` | Diff two PDFs | 500 sats |
| `explain_code` | Explain code in plain English | 500 sats |
| `memory_store` | Store persistent key-value memory | 10 sats |
| `memory_retrieve` | Read stored memory | 5 sats |
| `memory_delete` | Delete a memory key | free |
| `run_workflow` | Chain services in a single pipeline | 1000 sats |

---

## Example prompts

Once configured, just talk to your AI naturally:

```
"Summarise this research paper: https://arxiv.org/pdf/2301.00000"

"Review the code at https://github.com/me/repo/blob/main/app.py"

"Translate this contract to Spanish: https://example.com/contract.pdf"

"Compare these two versions of our terms of service:
  v1: https://example.com/tos-v1.pdf
  v2: https://example.com/tos-v2.pdf"

"Remember that the last file I asked you to review was app.py 
 and the score was 7/10"
```

Your agent handles the payment automatically — you just see the result.

---

## Workflow pipelines

Chain multiple services in one call with `run_workflow`:

```
"Fetch the Bitcoin whitepaper, translate the summary to French,
 and store the translation in my agent memory"
```

The `$from` syntax passes outputs between steps:

```json
{
  "steps": [
    {"service": "pdf",       "input": {"url": "https://bitcoin.org/bitcoin.pdf"}},
    {"service": "translate", "input": {"text": {"$from": 0}, "target_language": "french"}}
  ]
}
```

---

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NWC_CONNECTION_STRING` | One of these two | Nostr Wallet Connect string from Alby/Mutiny |
| `PHOENIXD_URL` | One of these two | Phoenixd base URL e.g. `http://localhost:9740` |
| `PHOENIXD_PASSWORD` | With Phoenixd | Phoenixd HTTP Basic auth password |
| `BOLTWORK_GATEWAY` | Optional | Override gateway URL (default: `https://parsebit-lnd.fly.dev`) |

---

## Pricing

All prices are in satoshis (sats). At current rates, 1000 sats ≈ $0.60 — but this varies with Bitcoin's price.

There is no subscription, no monthly fee, no minimum spend. You pay exactly for what your agent uses, nothing more.

---

## How L402 works

1. Agent calls a Boltwork tool
2. `boltwork-mcp` sends the request to `parsebit-lnd.fly.dev`
3. Receives HTTP 402 with a Lightning invoice (e.g. 500 sats for PDF summarisation)
4. Pays the invoice via your configured wallet (NWC or Phoenixd)
5. Retries the request with the payment proof
6. Returns the result to your agent

The whole flow takes 1-3 seconds. Your agent sees only the final result.

---

## Try before you pay

Boltwork has free trial endpoints — no Lightning wallet needed:

```bash
curl -X POST https://parsebit.fly.dev/trial/review \
  -H "Content-Type: application/json" \
  -d '{"code": "def add(a, b): return a + b"}'
```

---

## Links

- [Boltwork API](https://parsebit.fly.dev) — the service this MCP server wraps
- [Agent spec](https://parsebit.fly.dev/agent-spec.md) — full API reference
- [L402 Index](https://402index.io) — directory of L402 services
- [MCP documentation](https://modelcontextprotocol.io) — learn about MCP
- [Cracked Minds](https://crackedminds.co.uk) — the team behind Boltwork

---

## License

MIT
