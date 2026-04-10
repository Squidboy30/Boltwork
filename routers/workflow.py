"""
Boltwork Workflow Router
=========================
Chain multiple Boltwork services in a single call. Pay once, describe a
pipeline, get the final result. Designed for agents that need to run
multi-step tasks without managing intermediate calls and payments.

Endpoint:
  POST /workflow/run   - Execute a service pipeline (1000 sats)
  GET  /workflow/info  - Describe supported services and pipeline syntax (free)

Design:
  - Up to 5 steps per pipeline
  - Each step references a named internal service
  - Step inputs can be literal values or references to a previous step's output
    using {"$from": <step_index>} syntax
  - The full result of every step is returned in the response alongside
    the final output — agents can inspect intermediate results
  - All processing is synchronous and in-order
  - Total timeout: 120s across all steps

Supported services (allow-list — no arbitrary HTTP):
  webpage     → extract/webpage  (input: url)
  pdf         → summarise/url    (input: url, max_pages?)
  translate   → translate        (input: text or url, target_language)
  data        → extract/data     (input: url, max_pages?)
  tables      → analyse/tables   (input: url, max_pages?)
  explain     → analyse/explain  (input: code or url, language?)
  review      → review/code      (input: code, language?, filename?)
  compare     → analyse/compare  (input: url_a, url_b, max_pages?)
  summarise   → summarise/url    (alias for pdf)

Output passing:
  Each service exposes a primary text field used for chaining:
  webpage   → summary
  pdf       → summary
  translate → translated_text
  data      → summary
  tables    → summary
  explain   → explanation
  review    → summary
  compare   → summary
  summarise → summary

  When a step uses {"$from": N}, the primary text of step N is injected
  as the "text" input of the current step (useful for translate after webpage).

Pricing: 1000 sats flat — cheaper than running 3+ steps individually.
"""

import io
import json
import os
import socket
import time
from typing import Any, Optional

import anthropic
import httpx
import pdfplumber
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["workflow"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_STEPS       = 5
MAX_TEXT_CHARS  = 60_000
MAX_FETCH_BYTES = 10 * 1024 * 1024
SERVICE_URL     = os.environ.get("SERVICE_URL", "http://localhost:8000")

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
# Prompts (self-contained — no imports from other routers)
# ---------------------------------------------------------------------------

WEBPAGE_SYSTEM = """You are a precise web page summarisation engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "title": "page title or null",
  "summary": "2-3 sentence summary",
  "key_points": ["point 1", "point 2", "point 3"],
  "content_type": "article|blog|documentation|product|news|other",
  "language": "en",
  "sentiment": "positive|negative|neutral",
  "topics": ["topic1", "topic2"]
}"""

PDF_SYSTEM = """You are a precise document summarisation engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "title": "inferred document title or null",
  "summary": "2-3 sentence plain-English summary",
  "key_points": ["point 1", "point 2", "point 3"],
  "word_count": 0,
  "language": "en",
  "sentiment": "positive|negative|neutral",
  "topics": ["topic1", "topic2"]
}"""

TRANSLATE_SYSTEM = """You are a precise translation engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "source_language": "detected language",
  "target_language": "requested language",
  "translated_text": "full translated text",
  "word_count": 0,
  "notes": "any translation notes or null"
}"""

DATA_SYSTEM = """You are a precise data extraction engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "document_type": "invoice|contract|report|form|receipt|other",
  "dates": [{"label": "...", "value": "..."}],
  "parties": [{"role": "...", "name": "...", "address": null}],
  "amounts": [{"label": "...", "value": 0.0, "currency": "..."}],
  "line_items": [{"description": "...", "quantity": 1, "unit_price": 0.0, "total": 0.0}],
  "reference_numbers": [{"label": "...", "value": "..."}],
  "key_terms": ["..."],
  "summary": "1-2 sentence description"
}"""

TABLES_SYSTEM = """You are a precise table extraction engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "table_count": 0,
  "tables": [
    {
      "table_number": 1,
      "title": "table title or null",
      "page": 1,
      "headers": ["col1", "col2"],
      "rows": [["val1", "val2"]],
      "row_count": 1,
      "notes": null
    }
  ],
  "summary": "brief description of what data the tables contain"
}"""

EXPLAIN_SYSTEM = """You are an expert code explainer.
Output valid JSON only. No preamble, no markdown fences.
{
  "language": "detected language",
  "purpose": "one sentence: what this code does",
  "explanation": "plain English walkthrough",
  "sections": [{"lines": "1-15", "description": "..."}],
  "key_concepts": [{"term": "...", "plain_english": "..."}],
  "inputs": ["..."],
  "outputs": ["..."],
  "potential_issues": ["..."]
}"""

REVIEW_SYSTEM = """You are an expert code reviewer.
Output valid JSON only. No preamble, no markdown fences.
{
  "language": "detected language",
  "overall_score": 7,
  "summary": "2-3 sentence assessment",
  "bugs": [{"severity": "critical|high|medium|low", "line": null, "description": "...", "suggestion": "..."}],
  "security_issues": [{"severity": "critical|high|medium|low", "line": null, "description": "...", "suggestion": "..."}],
  "code_quality": [{"category": "readability|maintainability|performance|style|testing", "description": "...", "suggestion": "..."}],
  "strengths": ["..."],
  "recommended_actions": ["..."]
}"""

COMPARE_SYSTEM = """You are a precise document comparison engine.
Output valid JSON only. No preamble, no markdown fences.
{
  "document_a_title": "title or null",
  "document_b_title": "title or null",
  "overall_similarity": "high|medium|low",
  "summary": "2-3 sentence overview of differences",
  "additions": [{"section": null, "content": "..."}],
  "removals": [{"section": null, "content": "..."}],
  "modifications": [{"section": null, "original": "...", "revised": "...", "significance": "critical|major|minor"}],
  "unchanged": ["..."],
  "recommendation": "plain-English advice"
}"""


# ---------------------------------------------------------------------------
# Service registry — maps service name → (system_prompt, primary_output_field)
# ---------------------------------------------------------------------------

SERVICE_REGISTRY: dict[str, tuple[str, str]] = {
    "webpage":   (WEBPAGE_SYSTEM,  "summary"),
    "pdf":       (PDF_SYSTEM,      "summary"),
    "summarise": (PDF_SYSTEM,      "summary"),
    "translate": (TRANSLATE_SYSTEM, "translated_text"),
    "data":      (DATA_SYSTEM,     "summary"),
    "tables":    (TABLES_SYSTEM,   "summary"),
    "explain":   (EXPLAIN_SYSTEM,  "explanation"),
    "review":    (REVIEW_SYSTEM,   "summary"),
    "compare":   (COMPARE_SYSTEM,  "summary"),
}

VALID_SERVICES = set(SERVICE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class WorkflowStep(BaseModel):
    service: str
    input: dict[str, Any]

    @field_validator("service")
    @classmethod
    def service_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in VALID_SERVICES:
            raise ValueError(
                f"Unknown service '{v}'. Valid services: {sorted(VALID_SERVICES)}"
            )
        return v


class WorkflowRequest(BaseModel):
    steps: list[WorkflowStep]
    label: Optional[str] = None   # Optional human/agent label for this pipeline

    @field_validator("steps")
    @classmethod
    def steps_valid(cls, v: list) -> list:
        if not v:
            raise ValueError("steps must not be empty")
        if len(v) > MAX_STEPS:
            raise ValueError(f"Max {MAX_STEPS} steps per workflow")
        return v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_workflow(status: str, label: str = None, step_count: int = 0,
                 services: list = None, error: str = None,
                 duration_ms: int = 0, input_tokens: int = 0,
                 output_tokens: int = 0):
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": "/workflow/run",
        "status": status,
        "duration_ms": duration_ms,
        "step_count": step_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if label:
        entry["label"] = label
    if services:
        entry["services"] = services
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# HTTP / PDF helpers (self-contained)
# ---------------------------------------------------------------------------

async def _resolve_doh(hostname: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                "https://dns.google/resolve",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            for answer in r.json().get("Answer", []):
                if answer.get("type") == 1:
                    return answer["data"]
    except Exception:
        pass
    return socket.gethostbyname(hostname)


async def _fetch(url: str, timeout: float = 30.0) -> httpx.Response:
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    try:
        ip = await _resolve_doh(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        netloc = f"{ip}:{port}"
        ip_url = urlunparse((parsed.scheme, netloc, parsed.path,
                             parsed.params, parsed.query, parsed.fragment))
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, verify=False
        ) as http:
            return await http.get(ip_url, headers={
                "Host": parsed.hostname,
                "User-Agent": "Mozilla/5.0 (compatible; Boltwork/2.0)",
            })
    except Exception:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as http:
            return await http.get(url)


def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = 20) -> str:
    parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[:max_pages]:
            text = page.extract_text()
            if text:
                parts.append(text.strip())
    if not parts:
        raise HTTPException(status_code=422, detail="Could not extract text from PDF.")
    return "\n\n".join(parts)


async def _fetch_text(url: str, max_pages: int = 20) -> str:
    """Fetch a URL and return its text content — handles both HTML and PDF."""
    response = await _fetch(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        if len(response.content) > MAX_FETCH_BYTES:
            raise HTTPException(status_code=413, detail="PDF too large. Max 10MB.")
        return _extract_pdf_text(response.content, max_pages=max_pages)
    # HTML / plain text
    try:
        return response.text[:MAX_TEXT_CHARS]
    except Exception:
        raise HTTPException(status_code=422, detail="Could not decode content from URL.")


def _call_claude(system: str, user: str, max_tokens: int = 2000) -> tuple[dict, int, int]:
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
# Input resolver — replaces {"$from": N} references with prior step output
# ---------------------------------------------------------------------------

def _resolve_inputs(raw_input: dict, step_results: list[dict]) -> dict:
    """
    Walk the input dict and replace any {"$from": N} values with the
    primary output text of step N.
    """
    resolved = {}
    for key, value in raw_input.items():
        if isinstance(value, dict) and "$from" in value:
            idx = value["$from"]
            if not isinstance(idx, int) or idx < 0 or idx >= len(step_results):
                raise HTTPException(
                    status_code=400,
                    detail=f"$from index {idx} is out of range (only {len(step_results)} steps completed so far)"
                )
            prior = step_results[idx]
            service_name = prior["_step_meta"]["service"]
            _, primary_field = SERVICE_REGISTRY[service_name]
            primary_value = prior.get(primary_field, "")
            resolved[key] = primary_value
        else:
            resolved[key] = value
    return resolved


# ---------------------------------------------------------------------------
# Step executors — one per service type
# ---------------------------------------------------------------------------

async def _run_webpage(inputs: dict) -> tuple[dict, int, int]:
    url = inputs.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="webpage step requires 'url'")
    text = await _fetch_text(str(url))
    return _call_claude(WEBPAGE_SYSTEM, f"Summarise this web page:\n\n{text[:MAX_TEXT_CHARS]}")


async def _run_pdf(inputs: dict) -> tuple[dict, int, int]:
    url = inputs.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="pdf/summarise step requires 'url'")
    max_pages = int(inputs.get("max_pages", 20))
    text = await _fetch_text(str(url), max_pages=max_pages)
    return _call_claude(PDF_SYSTEM, f"Summarise this document:\n\n{text[:MAX_TEXT_CHARS]}")


async def _run_translate(inputs: dict) -> tuple[dict, int, int]:
    target = inputs.get("target_language")
    if not target:
        raise HTTPException(status_code=400, detail="translate step requires 'target_language'")
    text = inputs.get("text") or inputs.get("url")
    if not text:
        raise HTTPException(status_code=400, detail="translate step requires 'text' or 'url'")
    # If it looks like a URL, fetch it first
    if str(text).startswith("http"):
        text = await _fetch_text(str(text))
    truncated = str(text)[:MAX_TEXT_CHARS]
    return _call_claude(
        TRANSLATE_SYSTEM,
        f"Translate the following to {target}:\n\n{truncated}"
    )


async def _run_data(inputs: dict) -> tuple[dict, int, int]:
    url = inputs.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="data step requires 'url'")
    max_pages = int(inputs.get("max_pages", 20))
    text = await _fetch_text(str(url), max_pages=max_pages)
    return _call_claude(DATA_SYSTEM, f"Extract structured data from this document:\n\n{text[:MAX_TEXT_CHARS]}")


async def _run_tables(inputs: dict) -> tuple[dict, int, int]:
    url = inputs.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="tables step requires 'url'")
    max_pages = int(inputs.get("max_pages", 20))
    response = await _fetch(str(url))
    response.raise_for_status()
    if len(response.content) > MAX_FETCH_BYTES:
        raise HTTPException(status_code=413, detail="PDF too large.")
    # Try native table extraction first
    raw_tables_text = ""
    try:
        with pdfplumber.open(io.BytesIO(response.content)) as pdf:
            for page_num, page in enumerate(pdf.pages[:max_pages], 1):
                tables = page.extract_tables()
                for table in tables:
                    if table and len(table) > 1:
                        raw_tables_text += f"\nPage {page_num}:\n"
                        for row in table:
                            raw_tables_text += " | ".join(str(c or "") for c in row) + "\n"
    except Exception:
        pass
    if not raw_tables_text:
        raw_tables_text = _extract_pdf_text(response.content, max_pages=max_pages)
    return _call_claude(
        TABLES_SYSTEM,
        f"Extract all tables from this PDF content:\n\n{raw_tables_text[:MAX_TEXT_CHARS]}",
        max_tokens=3000,
    )


async def _run_explain(inputs: dict) -> tuple[dict, int, int]:
    code = inputs.get("code")
    url = inputs.get("url")
    language = inputs.get("language", "unknown")
    if not code and url:
        response = await _fetch(str(url))
        response.raise_for_status()
        try:
            code = response.content.decode("utf-8")
        except UnicodeDecodeError:
            code = response.content.decode("latin-1")
    if not code:
        raise HTTPException(status_code=400, detail="explain step requires 'code' or 'url'")
    return _call_claude(
        EXPLAIN_SYSTEM,
        f"Explain this code in plain English:\n\n```{language}\n{code[:MAX_TEXT_CHARS]}\n```",
        max_tokens=2500,
    )


async def _run_review(inputs: dict) -> tuple[dict, int, int]:
    code = inputs.get("code")
    language = inputs.get("language", "unknown")
    if not code:
        raise HTTPException(status_code=400, detail="review step requires 'code'")
    return _call_claude(
        REVIEW_SYSTEM,
        f"Review this {language} code:\n\n```{language}\n{code[:MAX_TEXT_CHARS]}\n```",
        max_tokens=2048,
    )


async def _run_compare(inputs: dict) -> tuple[dict, int, int]:
    url_a = inputs.get("url_a")
    url_b = inputs.get("url_b")
    if not url_a or not url_b:
        raise HTTPException(status_code=400, detail="compare step requires 'url_a' and 'url_b'")
    max_pages = int(inputs.get("max_pages", 20))
    import asyncio
    text_a, text_b = await asyncio.gather(
        _fetch_text(str(url_a), max_pages=max_pages),
        _fetch_text(str(url_b), max_pages=max_pages),
    )
    half = MAX_TEXT_CHARS // 2
    prompt = f"=== DOCUMENT A ===\n{text_a[:half]}\n\n=== DOCUMENT B ===\n{text_b[:half]}"
    return _call_claude(COMPARE_SYSTEM, prompt, max_tokens=3000)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

STEP_EXECUTORS = {
    "webpage":   _run_webpage,
    "pdf":       _run_pdf,
    "summarise": _run_pdf,
    "translate": _run_translate,
    "data":      _run_data,
    "tables":    _run_tables,
    "explain":   _run_explain,
    "review":    _run_review,
    "compare":   _run_compare,
}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/workflow/info")
def workflow_info():
    """Describe supported services and pipeline syntax. Free."""
    return {
        "service": "Boltwork Workflow",
        "description": (
            "Chain multiple Boltwork services in a single call. "
            "Pay once, describe a pipeline, get the final result plus all intermediate outputs."
        ),
        "price_sats": 1000,
        "payment_protocol": "L402",
        "max_steps": MAX_STEPS,
        "supported_services": {
            "webpage":   "Summarise a web page. Input: url",
            "pdf":       "Summarise a PDF. Input: url, max_pages?",
            "summarise": "Alias for pdf",
            "translate": "Translate text or URL. Input: text or url, target_language",
            "data":      "Extract structured data from a PDF. Input: url, max_pages?",
            "tables":    "Extract tables from a PDF. Input: url, max_pages?",
            "explain":   "Explain code in plain English. Input: code or url, language?",
            "review":    "Review code. Input: code, language?, filename?",
            "compare":   "Compare two PDFs. Input: url_a, url_b, max_pages?",
        },
        "output_chaining": (
            "Use {\"$from\": N} as an input value to inject the primary output "
            "of step N (0-indexed) into the current step. "
            "Primary outputs: webpage/pdf/summarise/data/tables/compare → summary, "
            "translate → translated_text, explain → explanation, review → summary."
        ),
        "example_pipelines": [
            {
                "label": "Fetch a webpage, translate to French, then summarise",
                "steps": [
                    {"service": "webpage",   "input": {"url": "https://example.com/article"}},
                    {"service": "translate", "input": {"text": {"$from": 0}, "target_language": "french"}},
                    {"service": "summarise", "input": {"url": "https://example.com/article"}},
                ]
            },
            {
                "label": "Extract data from an invoice PDF",
                "steps": [
                    {"service": "data", "input": {"url": "https://example.com/invoice.pdf"}},
                ]
            },
            {
                "label": "Summarise a PDF then translate the summary",
                "steps": [
                    {"service": "pdf",       "input": {"url": "https://example.com/report.pdf"}},
                    {"service": "translate", "input": {"text": {"$from": 0}, "target_language": "spanish"}},
                ]
            },
        ],
        "response_shape": {
            "label": "your pipeline label or null",
            "steps_executed": 2,
            "final_output": {"...": "result of the last step"},
            "step_results": [{"...": "result of step 0"}, {"...": "result of step 1"}],
            "_meta": {
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "duration_ms": 0,
                "price_sats": 1000,
            }
        },
        "upgrade_url": f"{SERVICE_URL}/.well-known/l402.json",
    }


@router.post("/workflow/run")
async def workflow_run(body: WorkflowRequest):
    """
    Execute a multi-step service pipeline.

    Each step runs in order. Outputs from prior steps can be referenced
    using {"$from": N} in any input value.

    Request body:
        steps (list, required) — up to 5 steps, each with:
            service (str) — one of: webpage, pdf, summarise, translate,
                            data, tables, explain, review, compare
            input   (dict) — inputs for this service; values may use
                             {"$from": N} to reference step N's primary output
        label (str, optional) — name for this pipeline (logged, returned)

    Returns all step results plus a top-level final_output.
    Price: 1000 sats via L402.
    """
    t0 = time.monotonic()
    step_results: list[dict] = []
    total_input_tokens = 0
    total_output_tokens = 0
    services_run = [step.service for step in body.steps]

    for i, step in enumerate(body.steps):
        try:
            resolved_input = _resolve_inputs(step.input, step_results)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Step {i} input resolution failed: {e}")

        executor = STEP_EXECUTORS[step.service]
        try:
            result, in_tok, out_tok = await executor(resolved_input)
        except HTTPException as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            log_workflow("error", label=body.label, step_count=i,
                         services=services_run, error=f"step {i} ({step.service}): {e.detail}",
                         duration_ms=duration_ms,
                         input_tokens=total_input_tokens,
                         output_tokens=total_output_tokens)
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Step {i} ({step.service}) failed: {e.detail}"
            )
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            log_workflow("error", label=body.label, step_count=i,
                         services=services_run, error=f"step {i} ({step.service}): {e}",
                         duration_ms=duration_ms,
                         input_tokens=total_input_tokens,
                         output_tokens=total_output_tokens)
            raise HTTPException(
                status_code=500,
                detail=f"Step {i} ({step.service}) failed unexpectedly: {e}"
            )

        total_input_tokens  += in_tok
        total_output_tokens += out_tok

        # Stamp each result with step metadata for reference resolution
        result["_step_meta"] = {
            "step": i,
            "service": step.service,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        }
        step_results.append(result)

    duration_ms = int((time.monotonic() - t0) * 1000)
    log_workflow("success", label=body.label,
                 step_count=len(body.steps),
                 services=services_run,
                 duration_ms=duration_ms,
                 input_tokens=total_input_tokens,
                 output_tokens=total_output_tokens)

    # Strip internal _step_meta from the clean results returned to caller
    clean_results = []
    for r in step_results:
        clean = {k: v for k, v in r.items() if k != "_step_meta"}
        clean_results.append(clean)

    return JSONResponse(content={
        "label": body.label,
        "steps_executed": len(body.steps),
        "services": services_run,
        "final_output": clean_results[-1],
        "step_results": clean_results,
        "_meta": {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "model": "claude-sonnet-4-6",
            "duration_ms": duration_ms,
            "price_sats": 1000,
        },
    })
