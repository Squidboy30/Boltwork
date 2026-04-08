"""
Boltwork Tier 1 Extensions
============================
Three new paid endpoints:

  POST /extract/webpage  - Summarise any web page by URL (100 sats)
  POST /extract/data     - Extract structured data from a PDF (200 sats)
  POST /translate        - Translate text or document URL (150 sats)

Follows same pattern as routers/review.py. Drop into routers/ and add
two lines to main.py to activate.
"""

import io
import json
import os
import socket
import time
from typing import Optional

import anthropic
import httpx
import pdfplumber
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["extract-translate"])

# ---------------------------------------------------------------------------
# Anthropic client
# ---------------------------------------------------------------------------

_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TEXT_CHARS = 60_000
MAX_FETCH_BYTES = 10 * 1024 * 1024  # 10MB

SUPPORTED_LANGUAGES = [
    "english", "spanish", "french", "german", "italian", "portuguese",
    "dutch", "russian", "chinese", "japanese", "korean", "arabic",
    "hindi", "turkish", "polish", "swedish", "danish", "norwegian",
    "finnish", "czech", "romanian", "hungarian", "greek", "hebrew",
]

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

WEBPAGE_SYSTEM_PROMPT = """You are a precise web content summarisation engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Return this exact structure:
{
  "title": "page title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["point 1", "point 2", "point 3"],
  "content_type": "article|blog|documentation|product|news|forum|other",
  "word_count": 0,
  "language": "en",
  "sentiment": "positive|negative|neutral",
  "topics": ["topic1", "topic2"],
  "links_mentioned": ["any important URLs referenced in the content"]
}

Never include any text outside the JSON object."""

DATA_EXTRACTION_SYSTEM_PROMPT = """You are a precise structured data extraction engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Analyse the document and extract all structured data you can find. Return this exact structure:
{
  "document_type": "invoice|contract|report|form|receipt|letter|other",
  "dates": [{"label": "Invoice Date", "value": "2024-01-15"}],
  "parties": [{"role": "Vendor", "name": "Acme Corp", "address": null, "email": null, "phone": null}],
  "amounts": [{"label": "Total Amount", "value": 1234.56, "currency": "GBP"}],
  "line_items": [{"description": "item", "quantity": 1, "unit_price": 100.0, "total": 100.0}],
  "reference_numbers": [{"label": "Invoice Number", "value": "INV-001"}],
  "key_terms": ["any important terms, conditions, or clauses"],
  "summary": "1-2 sentence description of what this document is",
  "_meta": {}
}

If a field has no data, use an empty array [] or null as appropriate.
Never include any text outside the JSON object."""

TRANSLATION_SYSTEM_PROMPT = """You are a precise translation engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Return this exact structure:
{
  "source_language": "detected source language name",
  "target_language": "target language name",
  "translated_text": "the full translated text",
  "word_count": 0,
  "notes": "any translation notes or ambiguities, or null"
}

Preserve formatting, paragraph breaks, and structure in the translation.
Never include any text outside the JSON object."""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class WebpageRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


class DataExtractionRequest(BaseModel):
    url: str
    max_pages: Optional[int] = 20

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


class TranslationRequest(BaseModel):
    text: Optional[str] = None
    url: Optional[str] = None
    target_language: str
    max_pages: Optional[int] = 10

    @field_validator("target_language")
    @classmethod
    def validate_language(cls, v):
        v = v.strip().lower()
        if v not in SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Unsupported language. Supported: {', '.join(SUPPORTED_LANGUAGES)}"
            )
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log_call(endpoint: str, status: str, error: str = None,
             duration_ms: int = 0, source_url: str = None,
             input_tokens: int = 0, output_tokens: int = 0):
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if source_url:
        entry["source_url"] = source_url
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


async def fetch_url(url: str, timeout: float = 30.0) -> httpx.Response:
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
            return await http.get(ip_url, headers={
                "Host": hostname,
                "User-Agent": "Mozilla/5.0 (compatible; Boltwork/2.0)",
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
            })
    except Exception:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            return await http.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; Boltwork/2.0)",
            })


def extract_text_from_html(html: str) -> str:
    """Simple HTML text extraction without external dependencies."""
    import re
    # Remove scripts and styles
    html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block elements with newlines
    html = re.sub(r'<(br|p|div|h[1-6]|li|tr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    # Strip remaining tags
    html = re.sub(r'<[^>]+>', ' ', html)
    # Decode common entities
    html = html.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>') \
               .replace('&nbsp;', ' ').replace('&quot;', '"').replace('&#39;', "'")
    # Collapse whitespace
    html = re.sub(r'\n{3,}', '\n\n', html)
    html = re.sub(r'[ \t]+', ' ', html)
    return html.strip()


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


def call_claude(system: str, user: str, max_tokens: int = 1500) -> tuple[dict, int, int]:
    """Call Claude and return (parsed_dict, input_tokens, output_tokens)."""
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = "\n".join(
            l for l in response_text.splitlines() if not l.startswith("```")
        ).strip()
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Model returned malformed JSON. Please retry.")
    return result, message.usage.input_tokens, message.usage.output_tokens


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/extract/webpage")
async def extract_webpage(body: WebpageRequest):
    """
    Summarise any web page by URL.

    Request body:
        url (str, required) - URL of the web page to summarise

    Returns structured JSON summary including title, key points, topics, sentiment.
    Price: 100 sats via L402.
    """
    t0 = time.monotonic()

    try:
        response = await fetch_url(str(body.url))
        response.raise_for_status()
    except httpx.HTTPError as e:
        log_call("/extract/webpage", "error", error=f"fetch failed: {e}",
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    if len(response.content) > MAX_FETCH_BYTES:
        raise HTTPException(status_code=413, detail="Page too large. Max 10MB.")

    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type or "text" in content_type:
        try:
            html = response.content.decode("utf-8", errors="replace")
        except Exception:
            html = response.text
        text = extract_text_from_html(html)
    else:
        raise HTTPException(status_code=415,
                            detail="URL does not appear to be a web page. Use /summarise/url for PDFs.")

    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not extract text from the page.")

    truncated = text[:MAX_TEXT_CHARS]

    try:
        result, input_tokens, output_tokens = call_claude(
            WEBPAGE_SYSTEM_PROMPT,
            f"Summarise this web page content:\n\nURL: {body.url}\n\n{truncated}"
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "url": str(body.url),
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/extract/webpage", "success", source_url=str(body.url),
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/extract/webpage", "error", error=e.detail,
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/extract/webpage", "error", error=str(e),
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/extract/data")
async def extract_data(body: DataExtractionRequest):
    """
    Extract structured data from a PDF document.

    Extracts: dates, parties, amounts, line items, reference numbers, key terms.
    Useful for: invoices, contracts, receipts, forms, reports.

    Request body:
        url       (str, required) - URL of the PDF
        max_pages (int, optional) - max pages to process (default 20)

    Returns structured JSON with all extracted data fields.
    Price: 200 sats via L402.
    """
    t0 = time.monotonic()

    try:
        response = await fetch_url(str(body.url))
        response.raise_for_status()
    except httpx.HTTPError as e:
        log_call("/extract/data", "error", error=f"fetch failed: {e}",
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" not in content_type and not str(body.url).lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="URL does not appear to be a PDF.")

    if len(response.content) > MAX_FETCH_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Max 10MB.")

    try:
        text = extract_text_from_pdf_bytes(response.content, max_pages=body.max_pages)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not read PDF: {e}")

    truncated = text[:MAX_TEXT_CHARS]

    try:
        result, input_tokens, output_tokens = call_claude(
            DATA_EXTRACTION_SYSTEM_PROMPT,
            f"Extract all structured data from this document:\n\n{truncated}",
            max_tokens=2000,
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "source_url": str(body.url),
            "pages_processed": min(body.max_pages, 20),
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/extract/data", "success", source_url=str(body.url),
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/extract/data", "error", error=e.detail,
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/extract/data", "error", error=str(e),
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/translate")
async def translate(body: TranslationRequest):
    """
    Translate text or a document (PDF/webpage) to a target language.

    Provide either:
        text (str) - text to translate directly
        url  (str) - URL of a PDF or web page to fetch and translate

    Request body:
        text            (str, optional) - text to translate
        url             (str, optional) - URL of document/page to translate
        target_language (str, required) - target language name (e.g. 'spanish', 'french')
        max_pages       (int, optional) - max PDF pages if using url (default 10)

    Returns structured JSON with translated text, source language, and notes.
    Price: 150 sats via L402.
    """
    t0 = time.monotonic()

    if not body.text and not body.url:
        raise HTTPException(status_code=400, detail="Provide either 'text' or 'url'.")

    source_url = str(body.url) if body.url else None
    input_text = body.text

    if body.url and not input_text:
        try:
            response = await fetch_url(str(body.url))
            response.raise_for_status()
        except httpx.HTTPError as e:
            log_call("/translate", "error", error=f"fetch failed: {e}",
                     source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

        if len(response.content) > MAX_FETCH_BYTES:
            raise HTTPException(status_code=413, detail="Document too large. Max 10MB.")

        content_type = response.headers.get("content-type", "").lower()
        if "pdf" in content_type or str(body.url).lower().endswith(".pdf"):
            try:
                input_text = extract_text_from_pdf_bytes(
                    response.content, max_pages=body.max_pages
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Could not read PDF: {e}")
        elif "html" in content_type or "text" in content_type:
            try:
                html = response.content.decode("utf-8", errors="replace")
            except Exception:
                html = response.text
            input_text = extract_text_from_html(html)
        else:
            raise HTTPException(status_code=415,
                                detail="URL must point to a PDF or web page.")

    if not input_text or not input_text.strip():
        raise HTTPException(status_code=422, detail="No text found to translate.")

    truncated = input_text[:MAX_TEXT_CHARS]

    try:
        result, input_tokens, output_tokens = call_claude(
            TRANSLATION_SYSTEM_PROMPT,
            f"Translate the following text to {body.target_language}:\n\n{truncated}",
            max_tokens=4000,
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "source_url": source_url,
            "truncated": len(input_text) > MAX_TEXT_CHARS,
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/translate", "success", source_url=source_url,
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/translate", "error", error=e.detail,
                 source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/translate", "error", error=str(e),
                 source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
