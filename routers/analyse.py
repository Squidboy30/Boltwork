"""
Boltwork Tier 2 Extensions
============================
Three new paid endpoints:

  POST /analyse/tables   - Extract tables from a PDF as structured JSON (300 sats)
  POST /analyse/compare  - Compare two PDFs and return a structured diff (500 sats)
  POST /analyse/explain  - Explain what code does in plain English (500 sats)

Follows same pattern as routers/extract.py. Drop into routers/ and add
two lines to main.py to activate.
"""

import io
import json
import os
import socket
import time
from typing import Optional, List

import anthropic
import httpx
import pdfplumber
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["analyse"])

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
MAX_CODE_CHARS = 80_000

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

TABLE_EXTRACTION_SYSTEM_PROMPT = """You are a precise table extraction engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Extract ALL tables found in the document. Return this exact structure:
{
  "table_count": 0,
  "tables": [
    {
      "table_number": 1,
      "title": "table title or null",
      "page": 1,
      "headers": ["column1", "column2", "column3"],
      "rows": [
        ["value1", "value2", "value3"],
        ["value4", "value5", "value6"]
      ],
      "row_count": 2,
      "notes": "any footnotes or context about this table, or null"
    }
  ],
  "summary": "brief description of what data the tables contain"
}

Rules:
- Extract every table present, even small ones
- Preserve original column headers exactly
- If a cell is empty, use null
- If a cell spans multiple columns, repeat the value
- Numbers should remain as strings to preserve formatting
- Never include any text outside the JSON object"""

COMPARISON_SYSTEM_PROMPT = """You are a precise document comparison engine.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Compare the two documents provided and return this exact structure:
{
  "document_a_title": "inferred title of first document or null",
  "document_b_title": "inferred title of second document or null",
  "overall_similarity": "high|medium|low",
  "summary": "2-3 sentence overview of how the documents differ",
  "additions": [
    {"section": "section name or null", "content": "content present in B but not A"}
  ],
  "removals": [
    {"section": "section name or null", "content": "content present in A but not B"}
  ],
  "modifications": [
    {
      "section": "section name or null",
      "original": "how it reads in document A",
      "revised": "how it reads in document B",
      "significance": "critical|major|minor"
    }
  ],
  "unchanged": ["list of major sections or topics that are the same in both"],
  "recommendation": "brief plain-English advice on what changed and what to pay attention to"
}

Never include any text outside the JSON object"""

EXPLANATION_SYSTEM_PROMPT = """You are an expert code explainer. Your job is to make code understandable to anyone.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

Return this exact structure:
{
  "language": "detected programming language",
  "purpose": "one sentence: what this code does overall",
  "explanation": "plain English walkthrough of what the code does, step by step. Write for a smart non-programmer.",
  "sections": [
    {
      "lines": "e.g. 1-15 or function name",
      "description": "what this section does in plain English"
    }
  ],
  "key_concepts": [
    {
      "term": "technical term used in the code",
      "plain_english": "what it means in plain English"
    }
  ],
  "inputs": ["what data or parameters this code takes"],
  "outputs": ["what this code produces or returns"],
  "potential_issues": ["any obvious problems, edge cases, or things to be aware of"],
  "_meta": {}
}

Write as if explaining to a smart business person who doesn't code.
Never use jargon without immediately explaining it.
Never include any text outside the JSON object"""


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TableExtractionRequest(BaseModel):
    url: str
    max_pages: Optional[int] = 20

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


class ComparisonRequest(BaseModel):
    url_a: str
    url_b: str
    max_pages: Optional[int] = 20

    @field_validator("url_a", "url_b")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


class ExplanationRequest(BaseModel):
    code: Optional[str] = None
    url: Optional[str] = None
    language: Optional[str] = None
    filename: Optional[str] = None

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("code must not be empty")
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
            })
    except Exception:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            return await http.get(url)


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


def extract_tables_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 20) -> list:
    """Extract tables using pdfplumber's native table extraction."""
    all_tables = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages[:max_pages], start=1):
            tables = page.extract_tables()
            for table in tables:
                if table and len(table) > 1:
                    all_tables.append({
                        "page": page_num,
                        "data": table
                    })
    return all_tables


def call_claude(system: str, user: str, max_tokens: int = 2000) -> tuple[dict, int, int]:
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


async def fetch_pdf_bytes(url: str) -> bytes:
    try:
        response = await fetch_url(url)
        response.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    content_type = response.headers.get("content-type", "").lower()
    if "pdf" not in content_type and not str(url).lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail="URL does not appear to be a PDF.")

    if len(response.content) > MAX_FETCH_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Max 10MB.")

    return response.content


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/analyse/tables")
async def extract_tables(body: TableExtractionRequest):
    """
    Extract all tables from a PDF as structured JSON.

    Useful for: financial reports, research data, invoices with line items,
    any PDF containing tabular data.

    Request body:
        url       (str, required) - URL of the PDF
        max_pages (int, optional) - max pages to process (default 20)

    Returns structured JSON with all tables, headers, and row data.
    Price: 300 sats via L402.
    """
    t0 = time.monotonic()

    pdf_bytes = await fetch_pdf_bytes(str(body.url))

    # First try pdfplumber native table extraction
    raw_tables = extract_tables_from_pdf_bytes(pdf_bytes, max_pages=body.max_pages)

    # Build prompt with both native extraction and raw text for Claude to work with
    if raw_tables:
        tables_text = "Tables detected by PDF parser:\n\n"
        for i, t in enumerate(raw_tables, 1):
            tables_text += f"Table {i} (page {t['page']}):\n"
            for row in t["data"]:
                tables_text += " | ".join(str(cell or "") for cell in row) + "\n"
            tables_text += "\n"
    else:
        tables_text = "No tables detected by PDF parser. Attempting text-based extraction.\n\n"
        try:
            text = extract_text_from_pdf_bytes(pdf_bytes, max_pages=body.max_pages)
            tables_text += text[:MAX_TEXT_CHARS]
        except HTTPException:
            raise HTTPException(status_code=422, detail="No tables found in this PDF.")

    try:
        result, input_tokens, output_tokens = call_claude(
            TABLE_EXTRACTION_SYSTEM_PROMPT,
            f"Extract all tables from this PDF content:\n\n{tables_text[:MAX_TEXT_CHARS]}",
            max_tokens=3000,
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "source_url": str(body.url),
            "pages_processed": min(body.max_pages, 20),
            "raw_tables_found": len(raw_tables),
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/tables", "success", source_url=str(body.url),
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/tables", "error", error=e.detail,
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/tables", "error", error=str(e),
                 source_url=str(body.url), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/analyse/compare")
async def compare_documents(body: ComparisonRequest):
    """
    Compare two PDF documents and return a structured diff.

    Identifies additions, removals, and modifications between the two documents.
    Useful for: contract versions, policy updates, research paper revisions,
    terms and conditions changes.

    Request body:
        url_a     (str, required) - URL of the first PDF (original)
        url_b     (str, required) - URL of the second PDF (revised)
        max_pages (int, optional) - max pages per document (default 20)

    Returns structured JSON with additions, removals, modifications, and recommendation.
    Price: 500 sats via L402.
    """
    t0 = time.monotonic()

    # Fetch both PDFs concurrently
    import asyncio
    try:
        pdf_a_bytes, pdf_b_bytes = await asyncio.gather(
            fetch_pdf_bytes(str(body.url_a)),
            fetch_pdf_bytes(str(body.url_b)),
        )
    except HTTPException:
        raise
    except Exception as e:
        log_call("/analyse/compare", "error", error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=400, detail=f"Failed to fetch documents: {e}")

    try:
        text_a = extract_text_from_pdf_bytes(pdf_a_bytes, max_pages=body.max_pages)
        text_b = extract_text_from_pdf_bytes(pdf_b_bytes, max_pages=body.max_pages)
    except HTTPException:
        raise

    # Truncate each to half the limit to fit both in context
    half_limit = MAX_TEXT_CHARS // 2
    truncated_a = text_a[:half_limit]
    truncated_b = text_b[:half_limit]

    prompt = f"""Compare these two documents:

=== DOCUMENT A ===
{truncated_a}

=== DOCUMENT B ===
{truncated_b}"""

    try:
        result, input_tokens, output_tokens = call_claude(
            COMPARISON_SYSTEM_PROMPT,
            prompt,
            max_tokens=3000,
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "url_a": str(body.url_a),
            "url_b": str(body.url_b),
            "truncated": len(text_a) > half_limit or len(text_b) > half_limit,
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/compare", "success",
                 source_url=f"{body.url_a} vs {body.url_b}",
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/compare", "error", error=e.detail,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/compare", "error", error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/analyse/explain")
async def explain_code(body: ExplanationRequest):
    """
    Explain what code does in plain English.

    Unlike code review (which finds problems), this explains the purpose
    and behaviour of code to someone who doesn't write code.
    Useful for: understanding inherited code, due diligence, security audits,
    onboarding documentation.

    Provide either:
        code (str) - source code directly
        url  (str) - URL to a code file (GitHub/GitLab supported)

    Request body:
        code     (str, optional) - source code to explain
        url      (str, optional) - URL of code file
        language (str, optional) - override language detection
        filename (str, optional) - helps language detection

    Returns structured JSON explanation including purpose, walkthrough, key concepts.
    Price: 500 sats via L402.
    """
    t0 = time.monotonic()

    if not body.code and not body.url:
        raise HTTPException(status_code=400, detail="Provide either 'code' or 'url'.")

    code = body.code
    source_url = str(body.url) if body.url else None

    if body.url and not code:
        # Reuse the URL fetching logic from review router
        import re
        from urllib.parse import urlparse

        url = str(body.url)

        # Normalise GitHub/GitLab URLs
        match = re.match(r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
        if match:
            user, repo, path = match.groups()
            url = f"https://raw.githubusercontent.com/{user}/{repo}/{path}"

        match = re.match(r"(https://gitlab\.com/[^/]+/[^/]+)/-/blob/(.+)", url)
        if match:
            base, path = match.groups()
            url = f"{base}/-/raw/{path}"

        try:
            response = await fetch_url(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            log_call("/analyse/explain", "error", error=str(e),
                     source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

        if len(response.content) > MAX_FETCH_BYTES:
            raise HTTPException(status_code=413, detail="File too large. Max 10MB.")

        try:
            code = response.content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                code = response.content.decode("latin-1")
            except Exception:
                raise HTTPException(status_code=422, detail="Could not decode file as text.")

        # Detect language from URL
        path = urlparse(str(body.url)).path
        filename = path.split("/")[-1] if "/" in path else path
        if not body.filename:
            body = body.model_copy(update={"filename": filename})

    if not code or not code.strip():
        raise HTTPException(status_code=422, detail="No code found to explain.")

    truncated = code[:MAX_CODE_CHARS]
    language = body.language or "unknown"

    try:
        result, input_tokens, output_tokens = call_claude(
            EXPLANATION_SYSTEM_PROMPT,
            f"Explain this code in plain English:\n\n```{language}\n{truncated}\n```",
            max_tokens=2500,
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "source_url": source_url,
            "truncated": len(code) > MAX_CODE_CHARS,
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/explain", "success", source_url=source_url,
                 duration_ms=duration_ms, input_tokens=input_tokens, output_tokens=output_tokens)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/explain", "error", error=e.detail,
                 source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/explain", "error", error=str(e),
                 source_url=source_url, duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
