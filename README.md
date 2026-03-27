# Parsebit

AI-powered PDF summarisation API. Send a PDF, get structured JSON back. Pay-per-call via Bitcoin Lightning L402.

## What it does

Parsebit accepts a PDF document (by file upload or URL) and returns a structured JSON summary including title, key points, topics, sentiment, and word count. Powered by Claude Sonnet.

## Live API

```
https://parsebit.fly.dev
```

## Quick test

```bash
curl -X POST https://parsebit.fly.dev/summarise/url \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.cte.iup.edu/cte/Resources/PDF_TestPage.pdf"}'
```

## Response format

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

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/summarise/upload` | Upload a PDF file (multipart/form-data) |
| POST | `/summarise/url` | Summarise a PDF from a URL (JSON body) |
| GET | `/agent-spec.md` | Machine-readable service description for AI agents |
| GET | `/.well-known/l402.json` | L402 discovery endpoint |

Full docs at `https://parsebit.fly.dev/docs`

## Payment

Protocol: L402 (Bitcoin Lightning Network)  
Price: 50 satoshis per call (~£0.03)  

Lightning payment layer coming soon. Currently free to use.

## Agent discovery

Agents can fetch the service spec at:

```
https://parsebit.fly.dev/agent-spec.md
```

Listed on the 402 Index for automatic agent discovery.

## Data & privacy

- Text is extracted on Fly.io servers in London (UK)
- Extracted text is sent to Anthropic's API for summarisation
- Anthropic does not train on API data by default
- No document content is stored permanently
- Full privacy notice: `https://parsebit.fly.dev/agent-spec.md`

## Tech stack

- **FastAPI** — API framework
- **Claude Sonnet 4.6** — AI summarisation
- **pdfplumber** — PDF text extraction
- **Fly.io** — Hosting (London region)
- **L402** — Lightning payment protocol (coming soon)

## Local development

```bash
git clone https://github.com/Squidboy30/paresbit
cd paresbit
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
set ANTHROPIC_API_KEY=your-key-here
uvicorn main:app --reload
```

## Licence

MIT
