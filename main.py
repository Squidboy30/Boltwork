import io, json, os, socket, time
from datetime import datetime, timezone

import anthropic, httpx, pdfplumber
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from typing import Optional

from routers.review import router as review_router


def log_call(endpoint: str, status: str, result: dict = None, error: str = None,
             duration_ms: int = 0, file_size_bytes: int = 0, source_url: str = None):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
    }
    if file_size_bytes:
        entry["file_size_bytes"] = file_size_bytes
    if source_url:
        entry["source_url"] = source_url
    if result:
        entry["language"] = result.get("language")
        entry["sentiment"] = result.get("sentiment")
        entry["word_count"] = result.get("word_count")
        entry["topics"] = result.get("topics", [])
        meta = result.get("_meta", {})
        entry["input_tokens"] = meta.get("input_tokens", 0)
        entry["output_tokens"] = meta.get("output_tokens", 0)
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


async def resolve_hostname_doh(hostname: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                "https://dns.google/resolve",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            data = response.json()
            for answer in data.get("Answer", []):
                if answer.get("type") == 1:
                    return answer["data"]
    except Exception:
        pass
    return socket.gethostbyname(hostname)


async def fetch_pdf_from_url(url: str, timeout: float = 30.0) -> httpx.Response:
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    hostname = parsed.hostname
    try:
        ip = await resolve_hostname_doh(hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        netloc = f"{ip}:{port}"
        ip_url = urlunparse((parsed.scheme, netloc, parsed.path,
                             parsed.params, parsed.query, parsed.fragment))
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=False,
        ) as http:
            return await http.get(ip_url, headers={"Host": hostname})
    except Exception:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            return await http.get(url)


SERVICE_URL = os.environ.get("SERVICE_URL", "http://localhost:8000")

app = FastAPI(
    title="Boltwork API",
    description="Autonomous agent-native AI services. Pay-per-call via Bitcoin Lightning L402.",
    version="2.0.0",
)

app.include_router(review_router)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

SYSTEM_PROMPT = """You are a precise document summarisation engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Return this exact structure:
{
  "title": "inferred document title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["point 1", "point 2", "point 3"],
  "word_count": 0,
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["topic1", "topic2"]
}

Never include any text outside the JSON object."""


class UrlRequest(BaseModel):
    url: str
    max_pages: Optional[int] = 20


def extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 20) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text()
            if text:
                text_parts.append(text.strip())
    if not text_parts:
        raise HTTPException(status_code=422, detail="Could not extract text from PDF.")
    return "\n\n".join(text_parts)


def summarise_text(text: str) -> dict:
    truncated = text[:60000]
    if len(text) > 60000:
        truncated += "\n\n[Document truncated for summarisation]"
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Summarise this document:\n\n{truncated}"}],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = "\n".join(
            l for l in response_text.splitlines() if not l.startswith("```")
        ).strip()
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Model returned malformed JSON.")
    result["_meta"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "model": message.model,
    }
    return result


@app.get("/health")
def health():
    return {"status": "ok", "version": "2.0.0", "service": "Boltwork"}


@app.get("/agent-spec.md", response_class=PlainTextResponse)
def agent_spec():
    return f"""# Boltwork API

## What this service does
Boltwork is an autonomous agent-native AI services API. Agents discover,
pay, and use services autonomously over Bitcoin Lightning — no accounts,
no API keys, no subscriptions.

### PDF Summarisation
Accepts a PDF document and returns a structured JSON summary.
Useful for: research papers, reports, contracts, or any PDF.

### Code Review
Accepts source code and returns a structured JSON review including bugs,
security issues, code quality observations, and recommended actions.
Supports: Python, JavaScript, TypeScript, Go, Rust, Java, C/C++, C#,
Ruby, PHP, Swift, Kotlin, Scala, Shell, SQL, Terraform, and more.

## Payment Protocol: L402 (Bitcoin Lightning Network)
- PDF Summarisation: 500 satoshis per call
- Code Review: 2000 satoshis per call

No account, signup, or API key required. Any Lightning-capable agent
can use this service autonomously.

## Base URL
{SERVICE_URL}

## Endpoints

### Summarisation
POST {SERVICE_URL}/summarise/upload  - Upload a PDF file (multipart/form-data, field: file)
POST {SERVICE_URL}/summarise/url     - Summarise PDF from URL (JSON body: url, max_pages)

### Code Review
POST {SERVICE_URL}/review/code       - Review code as text (JSON body: code, language?, filename?)
POST {SERVICE_URL}/review/url        - Review code from URL (JSON body: url, language?)
                                       Supports GitHub/GitLab blob URLs (auto-converted to raw)

### Utility
GET  {SERVICE_URL}/health            - Health check (free)
GET  {SERVICE_URL}/agent-spec.md     - This file (free)

## Summarisation response format
{{
  "title": "string or null",
  "summary": "2-3 sentence summary",
  "key_points": ["string", "string", "string"],
  "word_count": 1234,
  "language": "en",
  "sentiment": "positive | negative | neutral",
  "topics": ["string", "string"],
  "_meta": {{"input_tokens": 0, "output_tokens": 0, "model": ""}}
}}

## Code Review response format
{{
  "language": "python",
  "overall_score": 7,
  "summary": "2-3 sentence assessment",
  "bugs": [
    {{"severity": "critical|high|medium|low", "line": 42,
      "description": "...", "suggestion": "..."}}
  ],
  "security_issues": [
    {{"severity": "critical|high|medium|low", "line": 10,
      "description": "...", "suggestion": "..."}}
  ],
  "code_quality": [
    {{"category": "readability|maintainability|performance|style|testing",
      "description": "...", "suggestion": "..."}}
  ],
  "strengths": ["what the code does well"],
  "recommended_actions": ["prioritised fix list"],
  "_meta": {{"input_tokens": 0, "output_tokens": 0, "model": "", "truncated": false}}
}}

## Error codes
400 - Could not fetch the URL
413 - File/code too large
415 - Not a PDF (summarise endpoints)
422 - PDF has no extractable text / binary file (review endpoints)
500 - AI error, retry once

## L402 payment flow
1. Make your request normally.
2. Receive HTTP 402 with a Lightning invoice in the WWW-Authenticate header.
3. Pay the invoice with any Lightning wallet or L402-compatible client.
4. Retry with: Authorization: L402 <token>:<preimage>
5. Receive your JSON response.

## Agent discovery
Boltwork is discoverable by any agent that supports the L402 protocol:
- Well-known endpoint: {SERVICE_URL}/.well-known/l402.json
- Listed on the 402 Index: https://402index.io

## Data & privacy
- Documents/code processed on Fly.io servers in London (UK)
- Text sent to Anthropic's API for analysis
- Anthropic does not train on API data by default
- No content stored permanently by this service
- By Cracked Minds — crackedminds.co.uk
"""


@app.get("/.well-known/l402.json")
def l402_well_known():
    return {
        "version": "1.0",
        "name": "Boltwork",
        "description": "Autonomous agent-native AI services via Bitcoin Lightning. PDF summarisation and code review.",
        "url": SERVICE_URL,
        "spec": f"{SERVICE_URL}/agent-spec.md",
        "pricing": [
            {
                "endpoint": "/summarise/upload",
                "method": "POST",
                "price_sats": 500,
                "description": "Summarise uploaded PDF",
            },
            {
                "endpoint": "/summarise/url",
                "method": "POST",
                "price_sats": 500,
                "description": "Summarise PDF from URL",
            },
            {
                "endpoint": "/review/code",
                "method": "POST",
                "price_sats": 2000,
                "description": "Review code submitted as text",
            },
            {
                "endpoint": "/review/url",
                "method": "POST",
                "price_sats": 2000,
                "description": "Review code from URL (GitHub/GitLab/raw)",
            },
        ],
        "payment": {"protocol": "L402", "network": "lightning"},
        "contact": os.environ.get("CONTACT_EMAIL", ""),
        "provider": "Cracked Minds",
        "provider_url": "https://crackedminds.co.uk",
    }


@app.get("/.well-known/agent.json")
def agent_well_known():
    return l402_well_known()


@app.post("/summarise/upload")
async def summarise_upload(file: UploadFile = File(...)):
    t0 = time.monotonic()
    if not file.filename.lower().endswith(".pdf"):
        log_call("/summarise/upload", "error", error="not a PDF")
        raise HTTPException(status_code=415, detail="Only PDF files are supported.")
    pdf_bytes = await file.read()
    if len(pdf_bytes) > 10 * 1024 * 1024:
        log_call("/summarise/upload", "error", error="file too large",
                 file_size_bytes=len(pdf_bytes))
        raise HTTPException(status_code=413, detail="File too large. Max 10MB.")
    try:
        result = summarise_text(extract_text_from_pdf_bytes(pdf_bytes))
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/summarise/upload", "success", result=result,
                 duration_ms=duration_ms, file_size_bytes=len(pdf_bytes))
        return JSONResponse(content=result)
    except HTTPException as e:
        log_call("/summarise/upload", "error", error=e.detail,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise


@app.post("/summarise/url")
async def summarise_url(body: UrlRequest):
    t0 = time.monotonic()
    try:
        response = await fetch_pdf_from_url(str(body.url))
        response.raise_for_status()
    except httpx.HTTPError as e:
        log_call("/summarise/url", "error", error=f"fetch failed: {e}",
                 source_url=str(body.url),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")
    except Exception as e:
        log_call("/summarise/url", "error", error=f"fetch failed: {e}",
                 source_url=str(body.url),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type and not str(body.url).lower().endswith(".pdf"):
        log_call("/summarise/url", "error", error="not a PDF",
                 source_url=str(body.url))
        raise HTTPException(status_code=415, detail="URL does not appear to be a PDF.")

    try:
        result = summarise_text(
            extract_text_from_pdf_bytes(response.content, max_pages=body.max_pages)
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/summarise/url", "success", result=result,
                 duration_ms=duration_ms, source_url=str(body.url),
                 file_size_bytes=len(response.content))
        return JSONResponse(content=result)
    except HTTPException as e:
        log_call("/summarise/url", "error", error=e.detail,
                 source_url=str(body.url),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise
@app.get("/.well-known/ai-plugin.json")
def ai_plugin():
    return {
        "schema_version": "v1",
        "name_for_human": "Boltwork",
        "name_for_model": "boltwork",
        "description_for_human": "PDF summarisation and code review via Bitcoin Lightning. Pay per use, no accounts.",
        "description_for_model": "Boltwork provides two AI services payable via Bitcoin Lightning L402 micropayments. Use summarise_upload or summarise_url to summarise PDF documents (500 sats each). Use review_code or review_url to get structured code reviews including bugs, security issues, and quality observations (2000 sats each). No API key required — pay via Lightning invoice.",
        "auth": {
            "type": "none"
        },
        "api": {
            "type": "openapi",
            "url": f"{SERVICE_URL}/openapi.json",
        },
        "logo_url": f"{SERVICE_URL}/static/logo.png",
        "contact_email": os.environ.get("CONTACT_EMAIL", "hello@crackedminds.co.uk"),
        "legal_info_url": "https://crackedminds.co.uk",
    }


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt():
    return f"""# Boltwork

> Autonomous agent-native AI services via Bitcoin Lightning. No accounts, no API keys.

Boltwork provides pay-per-use AI services accessible to any Lightning-capable agent using the L402 protocol. Agents pay in Bitcoin satoshis per request with no registration required.

## Services

### PDF Summarisation — 500 sats per request
- POST {SERVICE_URL}/summarise/upload — Upload a PDF file
- POST {SERVICE_URL}/summarise/url — Summarise PDF from URL
- Returns: title, summary, key_points, word_count, language, sentiment, topics

### Code Review — 2000 sats per request
- POST {SERVICE_URL}/review/code — Submit code as text
- POST {SERVICE_URL}/review/url — Review code from URL (GitHub/GitLab supported)
- Returns: overall_score, bugs, security_issues, code_quality, strengths, recommended_actions

## Payment
Protocol: L402 (Bitcoin Lightning Network)
1. Make request → receive HTTP 402 with Lightning invoice
2. Pay invoice with any Lightning wallet
3. Retry with Authorization: L402 <token>:<preimage>
4. Receive JSON response

## Discovery
- Agent spec: {SERVICE_URL}/agent-spec.md
- L402 manifest: {SERVICE_URL}/.well-known/l402.json
- OpenAPI spec: {SERVICE_URL}/openapi.json
- 402 Index: https://402index.io

## Provider
Cracked Minds — crackedminds.co.uk
"""


@app.get("/.well-known/mcp.json")
def mcp_well_known():
    return {
        "name": "Boltwork",
        "version": "2.0.0",
        "description": "PDF summarisation and code review via Bitcoin Lightning L402 micropayments.",
        "tools": [
            {
                "name": "summarise_pdf_upload",
                "description": "Upload a PDF and receive an AI-generated structured summary. Costs 500 sats via Lightning.",
                "endpoint": f"{SERVICE_URL}/summarise/upload",
                "method": "POST",
                "price_sats": 500,
                "payment_protocol": "L402",
            },
            {
                "name": "summarise_pdf_url",
                "description": "Summarise a PDF from a URL. Costs 500 sats via Lightning.",
                "endpoint": f"{SERVICE_URL}/summarise/url",
                "method": "POST",
                "price_sats": 500,
                "payment_protocol": "L402",
            },
            {
                "name": "review_code",
                "description": "Submit code for an AI-powered review covering bugs, security issues, and quality. Costs 2000 sats via Lightning.",
                "endpoint": f"{SERVICE_URL}/review/code",
                "method": "POST",
                "price_sats": 2000,
                "payment_protocol": "L402",
            },
            {
                "name": "review_code_url",
                "description": "Review code from a URL (GitHub/GitLab supported). Costs 2000 sats via Lightning.",
                "endpoint": f"{SERVICE_URL}/review/url",
                "method": "POST",
                "price_sats": 2000,
                "payment_protocol": "L402",
            },
        ],
        "payment": {
            "protocol": "L402",
            "network": "lightning",
            "gateway": "https://parsebit-lnd.fly.dev",
        },
        "provider": "Cracked Minds",
        "provider_url": "https://crackedminds.co.uk",
    }
