"""
Boltwork Company Intelligence Router
======================================
UK company due diligence endpoint.

  POST /analyse/company  - Full AI risk report for any UK company (1000 sats)

Stub mode: when COMPANIES_HOUSE_API_KEY is not set, returns realistic mock data
so the full pipeline (Claude risk assessment, response shape) can be tested.
Set COMPANIES_HOUSE_API_KEY in Fly secrets to activate live data.
"""

import json
import os
import time
from typing import Optional

import base64
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

router = APIRouter(tags=["company"])

CH_BASE = "https://api.company-information.service.gov.uk"
MAX_DIRECTORS = 20

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
             input_tokens=0, output_tokens=0, company=None):
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if company:
        entry["company"] = company
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


class CompanyRequest(BaseModel):
    company_name: Optional[str] = None
    company_number: Optional[str] = None
    include_accounts: bool = True

    @field_validator("company_name")
    @classmethod
    def name_not_empty(cls, v):
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("company_name must not be empty")
        return v

    @field_validator("company_number")
    @classmethod
    def number_not_empty(cls, v):
        if v is not None:
            v = v.strip().upper().zfill(8)
        return v


def _ch_headers() -> dict:
    """Auth headers for Companies House API."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "")
    if not api_key:
        return {}
    auth = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {auth}"}


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def search_company(name: str) -> dict:
    """Search Companies House for a company by name. Returns top match."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/search/companies",
                params={"q": name, "items_per_page": 5},
                headers=_ch_headers()
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            if not items:
                raise HTTPException(status_code=404,
                                    detail=f"No company found matching \"{name}\"")
            return items[0]
    # STUB — replace when COMPANIES_HOUSE_API_KEY is set
    return {
        "company_number": "00445790",
        "title": "TESCO PLC",
        "company_status": "active",
        "company_type": "plc",
        "date_of_creation": "1947-11-27",
        "address": {
            "premises": "Tesco House Shire Park",
            "address_line_1": "Kestrel Way",
            "locality": "Welwyn Garden City",
            "postal_code": "AL7 1GA",
            "country": "England"
        }
    }


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_company_profile(number: str) -> dict:
    """Fetch full company profile from Companies House."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}",
                headers=_ch_headers()
            )
            r.raise_for_status()
            return r.json()
    # STUB — replace when COMPANIES_HOUSE_API_KEY is set
    return {
        "company_number": "00445790",
        "company_name": "TESCO PLC",
        "company_status": "active",
        "type": "plc",
        "date_of_creation": "1947-11-27",
        "registered_office_address": {
            "premises": "Tesco House Shire Park",
            "address_line_1": "Kestrel Way",
            "locality": "Welwyn Garden City",
            "postal_code": "AL7 1GA",
            "country": "England"
        },
        "accounts": {
            "next_due": "2025-06-30",
            "last_accounts": {"made_up_to": "2024-02-24", "type": "group"}
        },
        "has_insolvency_history": False,
        "has_charges": True,
        "registered_office_is_in_dispute": False
    }


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_directors(number: str) -> list:
    """Fetch officers from Companies House."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}/officers",
                params={"items_per_page": MAX_DIRECTORS},
                headers=_ch_headers()
            )
            r.raise_for_status()
            return r.json().get("items", [])
    # STUB — replace when COMPANIES_HOUSE_API_KEY is set
    return [
        {"name": "MURPHY, Ken", "officer_role": "director",
         "appointed_on": "2020-01-01", "resigned_on": None},
        {"name": "GRIFFIN, Imran", "officer_role": "director",
         "appointed_on": "2018-06-01", "resigned_on": None},
        {"name": "DAVID, George", "officer_role": "director",
         "appointed_on": "2015-03-15", "resigned_on": "2022-09-01"},
    ]


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_filing_history(number: str) -> dict:
    """Fetch filing history and identify late filings."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}/filing-history",
                params={"category": "accounts", "items_per_page": 10},
                headers=_ch_headers()
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            late_count = sum(
                1 for i in items
                if i.get("date") and i.get("action_date")
                and i["date"] > i.get("action_date", "")
            )
            latest = items[0] if items else None
            return {
                "latest_accounts_date": latest.get("action_date") if latest else None,
                "accounts_overdue": False,
                "late_filings_count": late_count,
                "latest_document_url": (
                    latest.get("links", {}).get("document_metadata") if latest else None
                )
            }
    # STUB — replace when COMPANIES_HOUSE_API_KEY is set
    return {
        "latest_accounts_date": "2024-02-24",
        "accounts_overdue": False,
        "late_filings_count": 0,
        "latest_document_url": None
    }


# SIC code descriptions mapping (most common)
SIC_DESCRIPTIONS = {
    "01": "Crop and animal production", "02": "Forestry", "03": "Fishing",
    "05": "Mining of coal", "06": "Extraction of crude petroleum",
    "10": "Manufacture of food products", "11": "Manufacture of beverages",
    "41": "Construction of buildings", "42": "Civil engineering",
    "45": "Wholesale/retail trade of motor vehicles",
    "46": "Wholesale trade", "47": "Retail trade",
    "49": "Land transport", "50": "Water transport", "51": "Air transport",
    "55": "Accommodation", "56": "Food and beverage service",
    "58": "Publishing", "59": "Film and video production",
    "60": "Broadcasting", "61": "Telecommunications",
    "62": "Computer programming", "63": "Information services",
    "64": "Financial service activities", "65": "Insurance",
    "66": "Activities auxiliary to financial services",
    "68": "Real estate activities", "69": "Legal and accounting",
    "70": "Management consultancy", "71": "Architecture and engineering",
    "72": "Scientific research", "73": "Advertising",
    "74": "Other professional activities", "75": "Veterinary activities",
    "77": "Rental and leasing", "78": "Employment activities",
    "79": "Travel agency", "80": "Security activities",
    "81": "Services to buildings", "82": "Office administrative activities",
    "84": "Public administration", "85": "Education",
    "86": "Human health activities", "87": "Residential care",
    "88": "Social work", "90": "Creative arts and entertainment",
    "91": "Libraries, museums", "92": "Gambling",
    "93": "Sports activities", "94": "Activities of membership organisations",
    "95": "Repair of computers", "96": "Other personal service activities",
    "97": "Activities of households as employers",
    "99": "Activities of extraterritorial organisations",
}


def describe_sic(code: str) -> str:
    """Return plain English description for a SIC code."""
    if not code:
        return "Unknown"
    prefix = code[:2]
    return SIC_DESCRIPTIONS.get(prefix, f"Industry code {code}")


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_persons_with_significant_control(number: str) -> list:
    """Fetch PSC (beneficial owners) from Companies House."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}/persons-with-significant-control",
                params={"items_per_page": 10},
                headers=_ch_headers()
            )
            if r.status_code == 200:
                return r.json().get("items", [])
        return []
    # STUB
    return [
        {
            "name": "TESCO HOLDINGS LIMITED",
            "kind": "corporate-entity-person-with-significant-control",
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
            "notified_on": "2016-04-06",
            "ceased": False,
        }
    ]


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_charges(number: str) -> list:
    """Fetch registered charges from Companies House."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}/charges",
                params={"items_per_page": 10},
                headers=_ch_headers()
            )
            if r.status_code == 200:
                items = r.json().get("items", [])
                return [
                    {
                        "description": i.get("classification", {}).get("description", "Charge"),
                        "created": i.get("created_on"),
                        "delivered": i.get("delivered_on"),
                        "status": i.get("status", "outstanding"),
                        "persons_entitled": [
                            p.get("name", "") for p in i.get("persons_entitled", [])
                        ],
                    }
                    for i in items[:10]
                ]
        return []
    # STUB
    return [
        {
            "description": "A registered charge",
            "created": "2020-01-15",
            "delivered": "2020-01-20",
            "status": "outstanding",
            "persons_entitled": ["HSBC UK BANK PLC"],
        }
    ]


# TODO: activate by setting COMPANIES_HOUSE_API_KEY in Fly secrets
async def get_previous_names(number: str) -> list:
    """Fetch previous company names from Companies House profile."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if api_key:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{CH_BASE}/company/{number}",
                headers=_ch_headers()
            )
            if r.status_code == 200:
                return r.json().get("previous_company_names", [])
        return []
    # STUB
    return []


RISK_SYSTEM_PROMPT = """You are a UK company risk assessment engine. \
Your output is always valid JSON — nothing else, no preamble, no markdown fences.

Given Companies House data for a UK company, produce a structured risk assessment.

Return this exact structure:
{
  "overall": "low|medium|high",
  "score": 3,
  "flags": ["red flags — e.g. late filings, dissolved status, insolvency history"],
  "positives": ["positive indicators — e.g. long trading history, filed on time"],
  "recommendation": "2-3 sentence plain English recommendation"
}

Score is 1-10 where 1=very low risk, 10=very high risk.
Never include text outside the JSON object."""


def call_claude_risk(company_data: dict) -> tuple:
    prompt = (
        "Assess the risk of this UK company based on the following data:\n\n"
        + json.dumps(company_data, indent=2)
    )
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        system=RISK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text.strip()
    if text.startswith("```"):
        text = "\n".join(
            l for l in text.splitlines() if not l.startswith("```")
        ).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500,
                            detail="Model returned malformed JSON. Please retry.")
    return result, message.usage.input_tokens, message.usage.output_tokens


@router.post("/analyse/company")
async def analyse_company(body: CompanyRequest):
    """
    Full AI risk assessment for any UK registered company.

    Provide either company_name or company_number (or both).
    Returns company profile, directors, filing history, and AI risk assessment.

    Request body:
        company_name     (str, optional) - company name to search
        company_number   (str, optional) - Companies House number (8 digits)
        include_accounts (bool, optional) - reserved for Phase 2 accounts analysis

    Price: 1000 sats via L402.
    """
    t0 = time.monotonic()

    if not body.company_name and not body.company_number:
        raise HTTPException(
            status_code=400,
            detail="Provide either company_name or company_number."
        )

    try:
        # Resolve company number
        if body.company_number:
            number = body.company_number
        else:
            search_result = await search_company(body.company_name)
            number = search_result.get("company_number", "")
            if not number:
                raise HTTPException(status_code=404,
                                    detail="Could not resolve company number.")

        # Fetch profile, directors, filings concurrently
        import asyncio
        profile, directors, filings, pscs, charges, prev_names = await asyncio.gather(
            get_company_profile(number),
            get_directors(number),
            get_filing_history(number),
            get_persons_with_significant_control(number),
            get_charges(number),
            get_previous_names(number),
        )

        # Build address string
        addr = profile.get("registered_office_address", {})
        address_str = ", ".join(p for p in [
            addr.get("premises", ""),
            addr.get("address_line_1", ""),
            addr.get("locality", ""),
            addr.get("postal_code", ""),
            addr.get("country", ""),
        ] if p)

        # Format directors
        formatted_directors = [
            {
                "name": d.get("name", "Unknown"),
                "role": d.get("officer_role", "director"),
                "appointed": d.get("appointed_on"),
                "resigned": d.get("resigned_on"),
            }
            for d in directors[:MAX_DIRECTORS]
        ]

        # Build Claude input
        # Extract SIC codes
        sic_codes = profile.get("sic_codes", [])
        nature_of_business = [describe_sic(s) for s in sic_codes] if sic_codes else []

        # Confirmation statement
        conf_stmt = profile.get("confirmation_statement", {})
        conf_overdue = conf_stmt.get("overdue", False)
        conf_next_due = conf_stmt.get("next_due")

        # Active charges count
        active_charges = [c for c in charges if c.get("status") == "outstanding"]

        company_data_for_claude = {
            "name": profile.get("company_name", body.company_name),
            "number": number,
            "status": profile.get("company_status", "unknown"),
            "type": profile.get("type", "unknown"),
            "incorporated": profile.get("date_of_creation"),
            "nature_of_business": nature_of_business,
            "has_insolvency_history": profile.get("has_insolvency_history", False),
            "active_charges_count": len(active_charges),
            "registered_office_in_dispute": profile.get(
                "registered_office_is_in_dispute", False
            ),
            "active_directors": len(
                [d for d in formatted_directors if not d["resigned"]]
            ),
            "resigned_directors": len(
                [d for d in formatted_directors if d["resigned"]]
            ),
            "previous_names_count": len(prev_names),
            "confirmation_statement_overdue": conf_overdue,
            "pscs_count": len(pscs),
            "filings": filings,
        }

        risk, input_tokens, output_tokens = call_claude_risk(company_data_for_claude)

        # Format PSCs
        formatted_pscs = [
            {
                "name": p.get("name", ""),
                "kind": p.get("kind", ""),
                "natures_of_control": p.get("natures_of_control", []),
                "notified_on": p.get("notified_on"),
                "ceased": p.get("ceased_on") is not None,
            }
            for p in pscs[:10]
        ]

        # Format previous names
        formatted_prev_names = [
            {
                "name": n.get("name", ""),
                "effective_from": n.get("effective_from"),
                "ceased_on": n.get("ceased_on"),
            }
            for n in prev_names[:10]
        ]

        result = {
            "company": {
                "name": profile.get("company_name", body.company_name or ""),
                "number": number,
                "status": profile.get("company_status", "unknown"),
                "type": profile.get("type", "unknown"),
                "incorporated": profile.get("date_of_creation"),
                "address": address_str,
                "nature_of_business": nature_of_business,
                "sic_codes": sic_codes,
                "previous_names": formatted_prev_names,
                "confirmation_statement": {
                    "next_due": conf_next_due,
                    "overdue": conf_overdue,
                    "last_made_up": profile.get(
                        "confirmation_statement", {}
                    ).get("last_made_up_to"),
                },
            },
            "directors": formatted_directors,
            "persons_with_significant_control": formatted_pscs,
            "charges": charges,
            "filings": filings,
            "accounts_summary": {
                "available": bool(filings.get("latest_document_url")),
                "pdf_url": filings.get("latest_document_url"),
                "summary": None,
                "key_figures": {
                    "turnover": None,
                    "net_assets": None,
                    "employees": None,
                }
            },
            "risk_assessment": risk,
            "_meta": {
                "companies_house_number": number,
                "accounts_pdf_url": filings.get("latest_document_url"),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "model": "claude-sonnet-4-6",
                "stub_mode": not bool(os.environ.get("COMPANIES_HOUSE_API_KEY")),
            }
        }

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_call("/analyse/company", "success",
                 duration_ms=duration_ms,
                 input_tokens=input_tokens,
                 output_tokens=output_tokens,
                 company=profile.get("company_name", body.company_name))
        return JSONResponse(content=result)

    except HTTPException as e:
        log_call("/analyse/company", "error", error=e.detail,
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_call("/analyse/company", "error", error=str(e),
                 duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
