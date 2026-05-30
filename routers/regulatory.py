"""
Boltwork Regulatory Intelligence Router
=========================================
UK regulatory search and document analysis.

  POST /analyse/regulatory  - FCA register search + AI risk summary (800 sats)
  POST /analyse/document    - Summarise any regulatory PDF with obligations extracted (600 sats)
"""

import json
import os
import time
from typing import Optional, List

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

router = APIRouter(tags=["regulatory"])

FCA_BASE = "https://register.fca.org.uk/services/V0.1"
FCA_EMAIL = os.environ.get("FCA_EMAIL", "api@crackedminds.co.uk")

_client = None

def get_client():
    global _client
    if _client is None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def log_call(endpoint, status, error=None, duration_ms=0,
             input_tokens=0, output_tokens=0, query=None):
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if query:
        entry["query"] = query
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


def fca_headers() -> dict:
    return {
        "X-AUTH-EMAIL": FCA_EMAIL,
        "Accept": "application/json",
        "User-Agent": "Boltwork/2.0 (crackedminds.co.uk)"
    }


class RegulatoryRequest(BaseModel):
    query: str
    search_type: Optional[str] = "firm"  # firm | individual

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        return v


class DocumentRequest(BaseModel):
    url: str
    doc_type: Optional[str] = None  # fca_policy | hmrc_guidance | ico_ruling | general

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


# ── FCA API helpers ──────────────────────────────────────────

async def fca_search_firm(query: str) -> list:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{FCA_BASE}/Firm/Search",
            params={"q": query, "page": 1},
            headers=fca_headers()
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("Data", [])[:5]
        return []


async def fca_firm_detail(frn: str) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        detail, individuals, permissions = await asyncio.gather(
            client.get(f"{FCA_BASE}/Firm/{frn}", headers=fca_headers()),
            client.get(f"{FCA_BASE}/Firm/{frn}/Individuals", headers=fca_headers()),
            client.get(f"{FCA_BASE}/Firm/{frn}/Permissions", headers=fca_headers()),
        )
        result = {}
        if detail.status_code == 200:
            result["profile"] = detail.json().get("Data", [{}])[0] if detail.json().get("Data") else {}
        if individuals.status_code == 200:
            result["individuals"] = individuals.json().get("Data", [])[:10]
        if permissions.status_code == 200:
            result["permissions"] = permissions.json().get("Data", [])[:20]
        return result


async def fca_search_individual(query: str) -> list:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{FCA_BASE}/Individual/Search",
            params={"q": query, "page": 1},
            headers=fca_headers()
        )
        if r.status_code == 200:
            return r.json().get("Data", [])[:5]
        return []


# ── Claude prompts ───────────────────────────────────────────

REGULATORY_RISK_PROMPT = """You are a UK financial regulatory risk assessment engine.
Your output is always valid JSON — nothing else, no preamble, no markdown fences.

Given FCA register data for a UK financial firm or individual, produce a structured assessment.

Return this exact structure:
{
  "overall_status": "authorised|unauthorised|cancelled|suspended|approved",
  "risk_level": "low|medium|high",
  "risk_score": 3,
  "summary": "2-3 sentence plain English summary of this entity's regulatory standing",
  "flags": ["list of red flags — cancelled permissions, regulatory notices, restrictions"],
  "positives": ["positive indicators — long authorisation, clean record, broad permissions"],
  "permissions_summary": "brief description of what this firm is authorised to do",
  "recommendation": "2-3 sentence plain English recommendation for someone considering engaging this firm"
}

Risk score 1-10 where 1=very low risk, 10=very high risk.
Never include text outside the JSON object."""


DOCUMENT_ANALYSIS_PROMPT = """You are a UK regulatory document analysis engine for compliance professionals.
Your output is always valid JSON — nothing else, no preamble, no markdown fences.

Analyse this regulatory document and extract structured information for a compliance officer.

Return this exact structure:
{
  "document_type": "fca_policy|hmrc_guidance|ico_ruling|pra_policy|legislation|other",
  "title": "document title",
  "published_date": "YYYY-MM-DD or null",
  "effective_date": "YYYY-MM-DD or null",
  "issuing_body": "FCA|HMRC|ICO|PRA|Treasury|other",
  "summary": "3-4 sentence plain English summary of what this document does",
  "key_obligations": [
    {"obligation": "what firms must do", "deadline": "date or null", "applies_to": "who this applies to"}
  ],
  "key_changes": ["list of changes from previous rules/guidance if applicable"],
  "affected_sectors": ["list of industry sectors affected"],
  "penalties": "description of penalties for non-compliance or null",
  "action_required": "yes|no|review",
  "action_summary": "what compliance teams need to do in response to this document",
  "risk_level": "low|medium|high",
  "_meta": {}
}

Write for a senior compliance officer at a UK financial services firm.
Never include text outside the JSON object."""


def call_claude(system: str, user: str, max_tokens: int = 1500) -> tuple:
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}]
    )
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith("```")).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Model returned malformed JSON. Please retry.")
    return result, message.usage.input_tokens, message.usage.output_tokens


async def fetch_document_text(url: str) -> str:
    """Fetch text content from a regulatory document URL (PDF or web page)."""
    import pdfplumber, io
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()

    content_type = r.headers.get("content-type", "").lower()

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            parts = [page.extract_text() for page in pdf.pages[:20] if page.extract_text()]
        return "\n\n".join(parts)[:50000]
    else:
        # HTML — strip tags
        import re
        text = re.sub(r'<[^>]+>', ' ', r.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:50000]


# ── Endpoints ────────────────────────────────────────────────

@router.post("/analyse/regulatory")
async def analyse_regulatory(body: RegulatoryRequest):
    """
    Search the FCA register and return an AI-powered regulatory risk assessment.

    Works for firms (by name or FRN) and approved individuals.
    Returns authorisation status, permissions summary, red flags, and recommendation.

    Request body:
        query       (str) - firm name, FRN number, or individual name
        search_type (str) - "firm" or "individual" (default: "firm")

    Price: 800 sats via L402.
    """
    t0 = time.monotonic()

    try:
        import asyncio

        if body.search_type == "individual":
            results = await fca_search_individual(body.query)
            entity_type = "individual"
        else:
            results = await fca_search_firm(body.query)
            entity_type = "firm"

        if not results:
            raise HTTPException(
                status_code=404,
                detail=f"No {entity_type} found matching '{body.query}' on the FCA register."
            )

        # Get detail for top result if it's a firm
        top = results[0]
        frn = str(top.get("FRN", top.get("frn", "")))
        detail = {}

        if entity_type == "firm" and frn:
            detail = await fca_firm_detail(frn)

        # Build Claude input
        fca_data = {
            "search_query": body.query,
            "entity_type": entity_type,
            "top_result": top,
            "detail": detail,
            "other_matches": results[1:] if len(results) > 1 else []
        }

        risk, input_tokens, output_tokens = call_claude(
            REGULATORY_RISK_PROMPT,
            f"Assess this FCA register entity:\n\n{json.dumps(fca_data, indent=2)}"
        )

        result = {
            "query": body.query,
            "entity_type": entity_type,
            "fca_results": results,
            "top_match": {
                "name": top.get("Organisation Name", top.get("Full Name", "")),
                "frn": frn,
                "status": top.get("Status", ""),
                "type": top.get("Business Type", ""),
            },
            "detail": detail,
            "risk_assessment": risk,
            "_meta": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": "claude-sonnet-4-6",
                "fca_results_count": len(results),
            }
        }

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/regulatory", "success",
                 duration_ms=duration_ms, input_tokens=input_tokens,
                 output_tokens=output_tokens, query=body.query)
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/regulatory", "error", error=e.detail,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/regulatory", "error", error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/analyse/document")
async def analyse_document(body: DocumentRequest):
    """
    Analyse any UK regulatory document — FCA policy, HMRC guidance, ICO ruling, legislation.

    Accepts a URL to a PDF or web page. Returns structured analysis including
    key obligations, deadlines, affected sectors, and action required.

    Request body:
        url      (str) - URL of the regulatory document
        doc_type (str, optional) - hint for document type

    Price: 600 sats via L402.
    """
    t0 = time.monotonic()

    try:
        text = await fetch_document_text(str(body.url))

        if not text.strip():
            raise HTTPException(status_code=422, detail="Could not extract text from document.")

        hint = f"Document type hint: {body.doc_type}\n\n" if body.doc_type else ""

        result, input_tokens, output_tokens = call_claude(
            DOCUMENT_ANALYSIS_PROMPT,
            f"{hint}Analyse this regulatory document:\n\n{text[:45000]}",
            max_tokens=2000
        )

        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "source_url": str(body.url),
            "text_length": len(text),
            "truncated": len(text) > 45000
        }

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/document", "success",
                 duration_ms=duration_ms, input_tokens=input_tokens,
                 output_tokens=output_tokens, query=str(body.url))
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/document", "error", error=e.detail,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/document", "error", error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
