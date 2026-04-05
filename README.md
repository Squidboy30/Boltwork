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
| PDF Summarisation | `POST /summarise/url` | 500 sats |
| PDF Summarisation | `POST /summarise/upload` | 500 sats |
| Code Review | `POST /review/url` | 2000 sats |
| Code Review | `POST /review/code` | 2000 sats |

## Quick test

```bash
curl -X POST https://parsebit.fly.dev/summarise/url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://bitcoin.org/bitcoin.pdf"}'
```

Returns `HTTP 402 Payment Required` with a Lightning invoice. Pay it, retry with credentials, get your result.

## Payment flow (L402)

```
1. POST /summarise/url          →  HTTP 402 + Lightning invoice (500 sats)
2. Pay invoice via any Lightning wallet
3. POST /summarise/url          →  HTTP 200 + JSON result
   Authorization: L402 <macaroon>:<preimage>
```

## PDF Summarisation response

```json
{
  "title": "Document title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["Point one", "Point two", "Point three"],
  "word_count": 1234,
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"],
  "_meta": {
    "input_tokens": 235,
    "output_tokens": 191,
    "model": "claude-sonnet-4-6"
  }
}
```

## Code Review response

```json
{
  "language": "python",
  "overall_score": 7,
  "summary": "2-3 sentence assessment",
  "bugs": [
    {"severity": "high", "line": 42, "description": "...", "suggestion": "..."}
  ],
  "security_issues": [
    {"severity": "critical", "line": 10, "description": "...", "suggestion": "..."}
  ],
  "code_quality": [
    {"category": "readability", "description": "...", "suggestion": "..."}
  ],
  "strengths": ["what the code does well"],
  "recommended_actions": ["prioritised fix list"],
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6", "truncated": false}
}
```

## Agent discovery

Boltwork is discoverable by any L402-compatible agent:

- Agent spec: `https://parsebit.fly.dev/agent-spec.md`
- Well-known: `https://parsebit.fly.dev/.well-known/l402.json`
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
git clone https://github.com/Squidboy30/parsebit
cd parsebit
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
