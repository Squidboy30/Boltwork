# Boltwork
**Autonomous agent-native AI services via Bitcoin Lightning.**

Boltwork is a pay-per-call API built for AI agents. Agents discover the service, pay a Lightning invoice, and get results back — no accounts, no API keys, no subscriptions. Fully autonomous machine-to-machine.

## Live API

```
https://parsebit.fly.dev
```

## Services

### Paid (L402 — Bitcoin Lightning)
| Service | Endpoint | Price |
|---------|----------|-------|
| PDF Summarisation (upload) | `POST /summarise/upload` | 500 sats |
| PDF Summarisation (URL) | `POST /summarise/url` | 500 sats |
| Code Review (direct) | `POST /review/code` | 2000 sats |
| Code Review (URL) | `POST /review/url` | 2000 sats |
| Web Page Summarisation | `POST /extract/webpage` | 100 sats |
| PDF Data Extraction | `POST /extract/data` | 200 sats |
| Translation (24 languages) | `POST /translate` | 150 sats |
| Table Extraction from PDF | `POST /analyse/tables` | 300 sats |
| Document Comparison | `POST /analyse/compare` | 500 sats |
| Code Explanation | `POST /analyse/explain` | 500 sats |
| Agent Memory Write | `POST /memory/store` | 10 sats |
| Agent Memory Read | `POST /memory/retrieve` | 5 sats |

### Free (no payment required)
| Service | Endpoint | Notes |
|---------|----------|-------|
| Trial Code Review | `POST /trial/review` | Real Claude output, 500 char cap, 5 calls/hr/IP |
| Trial Text Summary | `POST /trial/summarise` | Real Claude output, 1000 char cap, 5 calls/hr/IP |
| Trial Info | `GET /trial/info` | Limits and upgrade guide |
| Memory Delete | `POST /memory/delete` | Delete a stored key |
| Memory Info | `GET /memory/info` | Limits and pricing |

## Quick start — no Lightning required

Try the service instantly with the free trial endpoint:

```bash
curl -X POST https://parsebit.fly.dev/trial/review \
  -H "Content-Type: application/json" \
  -d '{"code": "def add(a, b): return a + b"}'
```

Returns a real Claude code review with no payment needed. When you're ready to remove the caps, upgrade to the paid endpoint via L402.

## Payment flow (L402)

```
1. POST /review/code            →  HTTP 402 + Lightning invoice (2000 sats)
2. Pay invoice via any Lightning wallet
3. POST /review/code            →  HTTP 200 + JSON result
   Authorization: L402 <macaroon>:<preimage>
```

## Agent Memory

Agents can persist structured context across sessions — no accounts needed. Keyed by a caller-supplied `agent_id`.

```bash
# Write
curl -X POST https://parsebit.fly.dev/memory/store \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "my-agent", "entries": {"last_file": "main.py", "status": "ok"}}'

# Read back
curl -X POST https://parsebit.fly.dev/memory/retrieve \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "my-agent"}'
```

Limits: 100 keys per agent, 128 char keys, 4096 char values, 10 keys per write call. Backed by SQLite on a persistent Fly.io volume.

## Response formats

### PDF Summarisation
```json
{
  "title": "Document title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["Point one", "Point two", "Point three"],
  "word_count": 1234,
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"],
  "_meta": {"input_tokens": 235, "output_tokens": 191, "model": "claude-sonnet-4-6"}
}
```

### Code Review
```json
{
  "language": "python",
  "overall_score": 7,
  "summary": "2-3 sentence assessment",
  "bugs": [{"severity": "high", "line": 42, "description": "...", "suggestion": "..."}],
  "security_issues": [{"severity": "critical", "line": 10, "description": "...", "suggestion": "..."}],
  "code_quality": [{"category": "readability", "description": "...", "suggestion": "..."}],
  "strengths": ["what the code does well"],
  "recommended_actions": ["prioritised fix list"],
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6", "truncated": false}
}
```

### Trial Review (free, capped)
```json
{
  "language": "python",
  "overall_score": 6,
  "summary": "1-2 sentence assessment",
  "top_issues": [{"severity": "low", "description": "...", "suggestion": "..."}],
  "strengths": ["one genuine strength if present"],
  "trial_note": "This is a capped trial review (500 char limit)...",
  "_meta": {"tier": "trial", "input_truncated": false, "rate_limit_remaining": 4, "upgrade_url": "..."}
}
```

### Web Page Summarisation
```json
{
  "title": "Page title",
  "summary": "2-3 sentence summary",
  "key_points": ["point 1", "point 2"],
  "content_type": "article|blog|documentation|product|news|other",
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"],
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6", "url": "..."}
}
```

### PDF Data Extraction
```json
{
  "document_type": "invoice|contract|report|form|receipt|other",
  "dates": [{"label": "Invoice Date", "value": "2024-01-15"}],
  "parties": [{"role": "Vendor", "name": "Acme Corp", "address": null}],
  "amounts": [{"label": "Total", "value": 1234.56, "currency": "GBP"}],
  "line_items": [{"description": "item", "quantity": 1, "unit_price": 100.0, "total": 100.0}],
  "reference_numbers": [{"label": "Invoice Number", "value": "INV-001"}],
  "key_terms": ["important terms or clauses"],
  "summary": "1-2 sentence description",
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6"}
}
```

### Translation
```json
{
  "source_language": "english",
  "target_language": "spanish",
  "translated_text": "the full translated text",
  "word_count": 42,
  "notes": "any translation notes or null",
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6"}
}
```

### Agent Memory Store
```json
{
  "status": "ok",
  "agent_id": "my-agent",
  "keys_written": ["last_file", "status"],
  "written_at": "2026-04-10T13:05:33Z",
  "keys_in_store": 2,
  "quota_remaining": 98,
  "_meta": {"tier": "paid", "price_sats": 10}
}
```

### Agent Memory Retrieve
```json
{
  "agent_id": "my-agent",
  "entries": {"last_file": "main.py", "status": "ok"},
  "updated_at": {"last_file": "2026-04-10T13:05:33Z", "status": "2026-04-10T13:05:33Z"},
  "keys_found": 2,
  "keys_requested": null,
  "_meta": {"tier": "paid", "price_sats": 5}
}
```

## Agent discovery
Boltwork is discoverable by any L402-compatible agent:
- Agent spec: `https://parsebit.fly.dev/agent-spec.md`
- L402 manifest: `https://parsebit.fly.dev/.well-known/l402.json`
- MCP discovery: `https://parsebit.fly.dev/.well-known/mcp.json`
- AI plugin: `https://parsebit.fly.dev/.well-known/ai-plugin.json`
- LLMs.txt: `https://parsebit.fly.dev/llms.txt`
- Listed on [402 Index](https://402index.io)

## Tech stack
- **FastAPI** — API framework
- **Claude Sonnet 4.6** — AI analysis
- **pdfplumber** — PDF text extraction
- **SQLite** — Agent memory persistence
- **Fly.io** — Hosting (London region, persistent volume for memory DB)
- **LND + Aperture** — Lightning L402 payment layer
- **ACINQ** — Lightning channel (400k sats)

## Local development
```bash
git clone https://github.com/Squidboy30/Boltwork
cd Boltwork
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
set ANTHROPIC_API_KEY=your-key-here
uvicorn main:app --reload
```

## Data & privacy
- Documents processed on Fly.io servers in London (UK)
- Text sent to Anthropic's API — not used for training by default
- No document content stored permanently
- Agent memory keys stored on Fly.io volume — delete any key at any time via `POST /memory/delete`

---
By [Cracked Minds](https://crackedminds.co.uk) · MIT Licence
