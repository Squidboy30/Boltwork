Boltwork
Autonomous agent-native AI services via Bitcoin Lightning.
Boltwork is a pay-per-call API built for AI agents. Agents discover the service, pay a Lightning invoice, and get results back — no accounts, no API keys, no subscriptions. Fully autonomous machine-to-machine.
Live Endpoints
AppURLPurposeAPIhttps://parsebit.fly.devFastAPI — all AI servicesGatewayhttps://parsebit-lnd.fly.devL402 payment layer (LND + Aperture)Landing pagehttps://crackedminds.co.uk/gatewayGateway-as-a-service productDashboardhttps://crackedminds.co.uk/gateway/dashboardLive admin metrics
Services
ServiceEndpointPricePDF Summarisation (upload)POST /summarise/upload500 satsPDF Summarisation (URL)POST /summarise/url500 satsCode Review (direct)POST /review/code2000 satsCode Review (URL)POST /review/url2000 satsWeb Page SummarisationPOST /extract/webpage100 satsPDF Data ExtractionPOST /extract/data200 satsTranslation (24 languages)POST /translate150 satsTable ExtractionPOST /analyse/tables300 satsDocument ComparisonPOST /analyse/compare500 satsCode ExplanationPOST /analyse/explain500 satsWorkflow PipelinePOST /workflow/run1000 satsAgent Memory WritePOST /memory/store10 satsAgent Memory ReadPOST /memory/retrieve5 satsAgent Memory DeletePOST /memory/deletefreeAgent SuggestPOST /suggestfreeTrial Code ReviewPOST /trial/reviewfree (rate limited)Trial SummarisePOST /trial/summarisefree (rate limited)
Quick test
bashcurl -X POST https://parsebit-lnd.fly.dev/extract/webpage \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
Returns HTTP 402 Payment Required with a Lightning invoice. Pay it, retry with credentials, get your result.
Payment flow (L402)
1. POST https://parsebit-lnd.fly.dev/extract/webpage
   → HTTP 402 + WWW-Authenticate: L402 macaroon="...", invoice="lnbc..."

2. Pay the Lightning invoice with any wallet (Strike, Phoenix, Alby etc.)

3. Retry with preimage:
   Authorization: L402 <macaroon>:<preimage>

4. → HTTP 200 + JSON result
Token reuse: A valid token+preimage pair can be reused for subsequent requests to the same endpoint — clients don't need to re-pay on retry. Tokens are scoped to a specific service and cannot be used across endpoints.
Invoice expiry: 24 hours (Aperture default).
Architecture
Two separate Fly.io apps:
parsebit.fly.dev          parsebit-lnd.fly.dev
─────────────────         ──────────────────────────
FastAPI (port 8000)       nginx (port 8079, public)
SQLite (/data/)      ←── Aperture (port 8080, internal)
                          LND (ports 9735/10009/8082)
                          supervisord (manages Aperture)
                          config-watcher (auto-provisions customers)
Important: parsebit-lnd is entirely separate from parsebit. nginx and Aperture configs must be edited via fly ssh console -a parsebit-lnd. Changes to the main repo have no effect on the Lightning gateway.
Gateway-as-a-Service
The L402 payment infrastructure is packaged as a managed product at crackedminds.co.uk/gateway.

Developers register their API and receive a gateway URL
Config watcher auto-provisions Aperture rules within 30 seconds
2% of transaction volume, no monthly fee, no minimum
Fee baked into Aperture price (price × 1.02, rounded up)
Billed monthly above 10,000 sat threshold

Lightning Node

Pubkey: 0383e0f6561bde4045994f99fdbcad4de8b260554b7f299be42e842f245751e7ec
Alias: boltwork
Address: 137.66.2.169:9735
Channel: 1 channel with ACINQ, 400,000 sat capacity
Inbound liquidity: ~49,848 sats

Response formats
PDF Summarisation
json{
  "title": "Document title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["Point one", "Point two", "Point three"],
  "word_count": 1234,
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"],
  "_meta": {"input_tokens": 235, "output_tokens": 191, "model": "claude-sonnet-4-6"}
}
Code Review
json{
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
Web Page Summarisation
json{
  "title": "Page title",
  "summary": "2-3 sentence summary",
  "key_points": ["point 1", "point 2"],
  "content_type": "article|blog|documentation|product|news|other",
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"],
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6", "url": "..."}
}
PDF Data Extraction
json{
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
Translation
json{
  "source_language": "english",
  "target_language": "spanish",
  "translated_text": "the full translated text",
  "word_count": 42,
  "notes": "any translation notes or null",
  "_meta": {"input_tokens": 0, "output_tokens": 0, "model": "claude-sonnet-4-6"}
}
Agent discovery
Boltwork is discoverable by any L402-compatible agent:

Agent spec: https://parsebit.fly.dev/agent-spec.md
L402 manifest: https://parsebit.fly.dev/.well-known/l402.json
MCP discovery: https://parsebit.fly.dev/.well-known/mcp.json
AI plugin: https://parsebit.fly.dev/.well-known/ai-plugin.json
LLMs.txt: https://parsebit.fly.dev/llms.txt
Listed on 402 Index
Gateway also serves: https://parsebit-lnd.fly.dev/.well-known/l402.json

Tech stack

FastAPI — API framework
Claude Sonnet 4.6 — AI analysis
pdfplumber — PDF text extraction
Fly.io — Hosting (London, lhr region)
LND + Aperture — Lightning L402 payment layer
supervisord — Process management for Aperture on parsebit-lnd
config-watcher — Auto-provisions Aperture config from gateway DB
SQLite — Gateway DB at /data/gateway.db, Aperture DB at /root/.lnd/.aperture/aperture.db
ACINQ — Lightning channel peer (400k sats)
nginx — Reverse proxy on parsebit-lnd, serves static well-known files

Key secrets
SecretAppPurposeANTHROPIC_API_KEYparsebitClaude APIGATEWAY_ADMIN_TOKENbothAdmin dashboard authFLY_API_TOKENparsebitFly logs API for dashboardSERVICE_URLparsebitSelf-reference URL
Local development
bashgit clone https://github.com/Squidboy30/Boltwork
cd Boltwork
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
set ANTHROPIC_API_KEY=your-key-here
uvicorn main:app --reload
Distribution

Stacker News: https://stacker.news/items/1473016/r/Squidboy30 — live, receiving traffic
HN Show HN: post written, pending inbound liquidity before posting
Reddit: post written, pending karma build before posting

Data & privacy

Documents processed on Fly.io servers in London (UK)
Text sent to Anthropic's API — not used for training by default
No content stored permanently


By Cracked Minds · MIT Licence
