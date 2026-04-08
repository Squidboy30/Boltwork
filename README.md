# Boltwork
**Autonomous agent-native AI services via Bitcoin Lightning.**

Boltwork is a pay-per-call API built for AI agents. Agents discover the service, pay a Lightning invoice, and get results back — no accounts, no API keys, no subscriptions. Fully autonomous machine-to-machine.

## Live API

```
https://parsebit.fly.dev
```

## Services
| Service | Endpoint | Price |
|---------|----------|-------|
| PDF Summarisation (upload) | `POST /summarise/upload` | 500 sats |
| PDF Summarisation (URL) | `POST /summarise/url` | 500 sats |
| Code Review (direct) | `POST /review/code` | 2000 sats |
| Code Review (URL) | `POST /review/url` | 2000 sats |
| Web Page Summarisation | `POST /extract/webpage` | 100 sats |
| PDF Data Extraction | `POST /extract/data` | 200 sats |
| Translation (24 languages) | `POST /translate` | 150 sats |

## Quick test
```bash
curl -X POST https://parsebit.fly.dev/summarise/url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://bitcoin.org/bitcoin.pdf"}'
```
Returns `HTTP 402 Payment Required` with a Lightning invoice. Pay it, retry with credentials, get your result.

## Payment flow (L402)

```
1. POST /extract/webpage        →  HTTP 402 + Lightning invoice (100 sats)
2. Pay invoice via any Lightning wallet
3. POST /extract/webpage        →  HTTP 200 + JSON result
Authorization: L402 <macaroon>:<preimage>
```

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
- **Fly.io** — Hosting (London region)
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
- No content stored permanently

---
By [Cracked Minds](https://crackedminds.co.uk) · MIT Licence