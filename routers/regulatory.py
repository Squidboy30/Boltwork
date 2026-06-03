"""
Boltwork Regulatory Intelligence Router
=========================================
UK regulatory search and document analysis.

  POST /analyse/regulatory  - Search Companies House for regulated firms + AI assessment
  POST /analyse/document    - Summarise any regulatory PDF/webpage with obligations extracted

Note: FCA register API requires IP whitelisting (submitted for approval).
Currently uses Companies House search + FCA register cross-reference.
"""

import json
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

router = APIRouter(tags=["regulatory"])

CH_BASE = "https://api.company-information.service.gov.uk"

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


def ch_headers() -> dict:
    import base64
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        return {}
    auth = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {auth}"}


class RegulatoryRequest(BaseModel):
    query: str
    search_type: Optional[str] = "firm"

    @field_validator("query")
    @classmethod
    def query_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        return v


class DocumentRequest(BaseModel):
    url: str
    doc_type: Optional[str] = None

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v




FCA_BASE = "https://register.fca.org.uk/services/V0.1"


def fca_headers() -> dict:
    """Headers for FCA register API - activates when FCA_API_KEY is set."""
    fca_key = os.environ.get("FCA_API_KEY", "")
    fca_email = os.environ.get("FCA_EMAIL", "tech@crackedminds.co.uk")
    if not fca_key:
        return {}
    return {
        "X-AUTH-EMAIL": fca_email,
        "X-AUTH-KEY": fca_key,
        "Accept": "application/json",
        "User-Agent": "Boltwork/2.0 (crackedminds.co.uk)"
    }


async def fca_search_firm(query: str) -> list:
    """Search FCA register directly when API key is active."""
    headers = fca_headers()
    if not headers:
        return []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{FCA_BASE}/Firm/Search",
                params={"q": query, "page": 1},
                headers=headers
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data.get("Data"), list):
                    return data["Data"]
        return []
    except Exception:
        return []


async def fca_firm_detail(frn: str) -> dict:
    """Get FCA firm detail when API key is active."""
    headers = fca_headers()
    if not headers:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{FCA_BASE}/Firm/{frn}", headers=headers)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data.get("Data"), list) and data["Data"]:
                    return data["Data"][0]
        return {}
    except Exception:
        return {}


async def search_companies_house(query: str, items: int = 10) -> list:
    """Search Companies House for firms matching query."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{CH_BASE}/search/companies",
            params={"q": query, "items_per_page": items},
            headers=ch_headers()
        )
        if r.status_code == 200:
            return r.json().get("items", [])
        return []


async def get_company_profile(number: str) -> dict:
    """Get full company profile from Companies House."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{CH_BASE}/company/{number}",
            headers=ch_headers()
        )
        if r.status_code == 200:
            return r.json()
        return {}


async def get_company_officers(number: str) -> list:
    """Get officers/directors from Companies House."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{CH_BASE}/company/{number}/officers",
            params={"items_per_page": 10},
            headers=ch_headers()
        )
        if r.status_code == 200:
            return r.json().get("items", [])
        return []


async def get_filing_history(number: str) -> dict:
    """Get filing history from Companies House."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{CH_BASE}/company/{number}/filing-history",
            params={"category": "accounts", "items_per_page": 5},
            headers=ch_headers()
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            latest = items[0] if items else None
            return {
                "latest_accounts_date": latest.get("action_date") if latest else None,
                "accounts_overdue": False,
                "late_filings_count": 0,
                "latest_document_url": latest.get("links", {}).get("document_metadata") if latest else None
            }
        return {"latest_accounts_date": None, "accounts_overdue": False, "late_filings_count": 0}


REGULATORY_RISK_PROMPT = """You are a UK financial regulatory compliance assessment engine.
Your output is always valid JSON — nothing else, no preamble, no markdown fences.

Given Companies House data for a UK company, assess its regulatory standing and risk profile
from a compliance perspective. Consider: company age, status, director history, filing compliance,
sector (if determinable from name/type), and any red flags.

Return this exact structure:
{
  "overall_status": "active|dissolved|dormant|unknown",
  "risk_level": "low|medium|high",
  "risk_score": 3,
  "sector_guess": "financial services|legal|accountancy|insurance|unknown",
  "likely_regulated": true,
  "summary": "2-3 sentence plain English summary of this company's compliance standing",
  "flags": ["list of red flags — late filings, dissolved status, frequent director changes"],
  "positives": ["positive indicators — long history, clean record, stable directors"],
  "fca_register_url": "https://register.fca.org.uk/s/search#q={company_name}&t=Companies&sort=relevance",
  "recommendation": "2-3 sentence plain English recommendation for someone conducting due diligence"
}

Score 1-10 where 1=very low risk, 10=very high risk.
For fca_register_url, replace {company_name} with the actual URL-encoded company name.
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
  "summary": "3-4 sentence plain English summary",
  "key_obligations": [
    {"obligation": "what firms must do", "deadline": "date or null", "applies_to": "who this applies to"}
  ],
  "key_changes": ["list of changes from previous rules if applicable"],
  "affected_sectors": ["list of industry sectors affected"],
  "penalties": "description of penalties or null",
  "action_required": "yes|no|review",
  "action_summary": "what compliance teams need to do",
  "risk_level": "low|medium|high"
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
    """Fetch and extract text from a regulatory document URL."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()

    content_type = r.headers.get("content-type", "").lower()

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            import pdfplumber, io
            with pdfplumber.open(io.BytesIO(r.content)) as pdf:
                parts = [page.extract_text() for page in pdf.pages[:20] if page.extract_text()]
            return "\n\n".join(parts)[:50000]
        except Exception:
            return r.text[:50000]
    else:
        import re
        text = re.sub(r'<[^>]+>', ' ', r.text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:50000]


@router.post("/analyse/regulatory")
async def analyse_regulatory(body: RegulatoryRequest):
    """
    Search for UK regulated firms and return AI-powered compliance assessment.

    Uses Companies House data with FCA register cross-reference link.
    Returns up to 10 matching companies with compliance risk assessment on top match.

    Request body:
        query       (str) - firm name or Companies House number
        search_type (str) - "firm" or "individual" (default: "firm")

    Price: 800 sats via L402.
    """
    t0 = time.monotonic()

    try:
        import asyncio

        # Try FCA register first if key is active, fall back to Companies House
        fca_results = await fca_search_firm(body.query)
        if fca_results:
            # FCA API active - enrich data with FCA details
            top_fca = fca_results[0]
            frn = str(top_fca.get("FRN", ""))
            fca_detail = await fca_firm_detail(frn) if frn else {}
        else:
            fca_results = []
            fca_detail = {}

        # Search Companies House for filing/officer data
        results = await search_companies_house(body.query, items=10)

        if not results:
            raise HTTPException(
                status_code=404,
                detail=f"No companies found matching '{body.query}'. Try a shorter or different name."
            )

        # Get full detail on top result
        top = results[0]
        number = top.get("company_number", "")

        profile, officers, filings = await asyncio.gather(
            get_company_profile(number),
            get_company_officers(number),
            get_filing_history(number),
        )

        # Build address string
        addr = profile.get("registered_office_address", {})
        address_str = ", ".join(p for p in [
            addr.get("premises", ""), addr.get("address_line_1", ""),
            addr.get("locality", ""), addr.get("postal_code", ""),
            addr.get("country", "")
        ] if p)

        # Format officers
        formatted_officers = [
            {
                "name": o.get("name", ""),
                "role": o.get("officer_role", ""),
                "appointed": o.get("appointed_on"),
                "resigned": o.get("resigned_on")
            }
            for o in officers[:10]
        ]

        company_data = {
            "name": profile.get("company_name", top.get("title", "")),
            "number": number,
            "status": profile.get("company_status", "unknown"),
            "type": profile.get("type", "unknown"),
            "incorporated": profile.get("date_of_creation"),
            "has_insolvency_history": profile.get("has_insolvency_history", False),
            "has_charges": profile.get("has_charges", False),
            "active_officers": len([o for o in formatted_officers if not o["resigned"]]),
            "resigned_officers": len([o for o in formatted_officers if o["resigned"]]),
            "filings": filings,
            "address": address_str,
        }

        assessment, input_tokens, output_tokens = call_claude(
            REGULATORY_RISK_PROMPT,
            f"Assess the regulatory compliance standing of this UK company:\n\n{json.dumps(company_data, indent=2)}"
        )

        result = {
            "query": body.query,
            "total_results": len(results),
            "all_matches": [
                {
                    "name": r.get("title", ""),
                    "number": r.get("company_number", ""),
                    "status": r.get("company_status", ""),
                    "type": r.get("company_type", ""),
                    "address": r.get("address_snippet", ""),
                    "incorporated": r.get("date_of_creation", ""),
                }
                for r in results
            ],
            "top_match": {
                "name": profile.get("company_name", top.get("title", "")),
                "number": number,
                "status": profile.get("company_status", "unknown"),
                "type": profile.get("type", "unknown"),
                "incorporated": profile.get("date_of_creation"),
                "address": address_str,
            },
            "officers": formatted_officers,
            "filings": filings,
            "risk_assessment": assessment,
            "_meta": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": "claude-sonnet-4-6",
                "data_source": "FCA Register + Claude AI" if fca_results else "Companies House + Claude AI",
                "fca_api_active": bool(fca_results),
                "fca_top_match": fca_results[0] if fca_results else None,
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
