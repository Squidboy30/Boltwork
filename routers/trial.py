"""
Boltwork Trial Router
======================
Free-tier endpoint for agent discovery and onboarding.
No L402 payment required — but strictly rate-limited and capped.

Endpoints:
  POST /trial/review    - Review up to 500 chars of code (free, capped)
  POST /trial/summarise - Summarise up to 1000 chars of text (free, capped)
  GET  /trial/info      - Explains trial limits and how to upgrade (free)

Design goals:
  - Zero friction for new agents discovering the service
  - Real Claude responses, not stubs — agents see genuine value
  - Hard caps enforced server-side (not just documented)
  - In-memory rate limiting: max 5 trial calls per IP per hour
  - Every response includes upgrade hints pointing to paid endpoints
  - No SQLite / external deps — pure stdlib + existing stack
"""

import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/trial", tags=["trial"])

# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton)
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

TRIAL_CODE_CHAR_LIMIT    = 500    # Hard cap on code input
TRIAL_TEXT_CHAR_LIMIT    = 1000   # Hard cap on text input
TRIAL_OUTPUT_TOKEN_LIMIT = 256    # Keep responses concise
RATE_LIMIT_CALLS         = 5      # Max calls per window
RATE_LIMIT_WINDOW_SEC    = 3600   # 1 hour

SERVICE_URL = os.environ.get("SERVICE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# In-memory rate limiter  {ip: [(timestamp, ...), ...]}
# ---------------------------------------------------------------------------

_rate_store: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> tuple[bool, int]:
    """
    Returns (allowed, calls_remaining).
    Prunes stale timestamps as a side effect.
    """
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SEC
    calls = [t for t in _rate_store[ip] if t > window_start]
    _rate_store[ip] = calls
    remaining = max(0, RATE_LIMIT_CALLS - len(calls))
    if len(calls) >= RATE_LIMIT_CALLS:
        return False, 0
    _rate_store[ip].append(now)
    return True, remaining - 1


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

TRIAL_REVIEW_SYSTEM_PROMPT = """You are a concise code reviewer. 
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

The code snippet is short (trial mode). Return this exact structure:
{
  "language": "detected language",
  "overall_score": 7,
  "summary": "1-2 sentence assessment",
  "top_issues": [
    {"severity": "critical|high|medium|low", "description": "issue", "suggestion": "fix"}
  ],
  "strengths": ["one genuine strength if present"],
  "trial_note": "This is a capped trial review (500 char limit). Full reviews available at /review/code for 2000 sats."
}

Keep top_issues to at most 3 items. Be direct and useful even for short snippets.
Never include text outside the JSON object."""

TRIAL_SUMMARISE_SYSTEM_PROMPT = """You are a concise text summariser.
Your output is always valid JSON - nothing else, no preamble, no markdown fences.

The text is short (trial mode). Return this exact structure:
{
  "summary": "1-2 sentence plain-English summary",
  "key_points": ["point 1", "point 2"],
  "sentiment": "positive|negative|neutral",
  "language": "en",
  "trial_note": "This is a capped trial summary (1000 char limit). Full PDF summarisation at /summarise/url for 500 sats."
}

Keep key_points to at most 2 items.
Never include text outside the JSON object."""

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TrialCodeRequest(BaseModel):
    code: str
    language: Optional[str] = None

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("code must not be empty")
        return v


class TrialTextRequest(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def text_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be empty")
        return v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_trial(endpoint: str, status: str, ip: str,
              error: str = None, duration_ms: int = 0,
              input_tokens: int = 0, output_tokens: int = 0,
              char_count: int = 0):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "char_count": char_count,
        "ip": ip,
        "tier": "trial",
    }
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Core Claude call
# ---------------------------------------------------------------------------

def _call_claude(system: str, user: str) -> tuple[dict, int, int]:
    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=TRIAL_OUTPUT_TOKEN_LIMIT,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    response_text = message.content[0].text.strip()
    if response_text.startswith("```"):
        response_text = "\n".join(
            line for line in response_text.splitlines()
            if not line.startswith("```")
        ).strip()
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=500,
            detail="Model returned malformed JSON. Please retry."
        )
    return result, message.usage.input_tokens, message.usage.output_tokens


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/info")
def trial_info():
    """
    Describe trial limits, paid tiers, and how to upgrade.
    Free — no payment, no rate limit.
    """
    return {
        "service": "Boltwork Trial",
        "description": (
            "Try Boltwork AI services free, no Lightning payment required. "
            "Trial responses are real Claude outputs with input size and rate caps."
        ),
        "trial_limits": {
            "rate_limit": f"{RATE_LIMIT_CALLS} calls per hour per IP",
            "code_review_input_cap": f"{TRIAL_CODE_CHAR_LIMIT} characters",
            "text_summary_input_cap": f"{TRIAL_TEXT_CHAR_LIMIT} characters",
            "output_tokens": TRIAL_OUTPUT_TOKEN_LIMIT,
        },
        "trial_endpoints": {
            "POST /trial/review":    "Free capped code review",
            "POST /trial/summarise": "Free capped text summary",
            "GET  /trial/info":      "This document (free)",
        },
        "paid_endpoints": {
            "POST /review/code":      "Full code review — 2000 sats via L402",
            "POST /review/url":       "Review code from URL — 2000 sats",
            "POST /summarise/url":    "Full PDF summarisation — 500 sats",
            "POST /summarise/upload": "Upload PDF to summarise — 500 sats",
            "POST /extract/webpage":  "Web page summary — 100 sats",
            "POST /extract/data":     "Structured data from PDF — 200 sats",
            "POST /translate":        "Translation (24 languages) — 150 sats",
            "POST /analyse/tables":   "Table extraction from PDF — 300 sats",
            "POST /analyse/compare":  "Compare two PDFs — 500 sats",
            "POST /analyse/explain":  "Code explanation (plain English) — 500 sats",
        },
        "payment_protocol": "L402 (Bitcoin Lightning Network)",
        "agent_spec": f"{SERVICE_URL}/agent-spec.md",
        "l402_manifest": f"{SERVICE_URL}/.well-known/l402.json",
    }


@router.post("/review")
async def trial_review(body: TrialCodeRequest, request: Request):
    """
    Free trial code review — real Claude response, capped at 500 chars input.

    Rate limited: {RATE_LIMIT_CALLS} calls per IP per hour.
    For full reviews (no cap, deeper analysis), use POST /review/code (2000 sats).

    Request body:
        code     (str, required) - code snippet to review (max 500 chars)
        language (str, optional) - language hint
    """
    t0 = time.monotonic()
    ip = _client_ip(request)

    allowed, remaining = _check_rate_limit(ip)
    if not allowed:
        log_trial("/trial/review", "rate_limited", ip=ip,
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Trial rate limit reached ({RATE_LIMIT_CALLS} calls/hour). "
                "Upgrade to paid via L402 for unlimited access: "
                f"{SERVICE_URL}/.well-known/l402.json"
            ),
        )

    # Hard-enforce the input cap — truncate silently
    code = body.code[:TRIAL_CODE_CHAR_LIMIT]
    was_truncated = len(body.code) > TRIAL_CODE_CHAR_LIMIT
    language = body.language or "unknown"

    user_prompt = f"Language: {language}\n\nReview this code:\n\n```{language}\n{code}\n```"
    if was_truncated:
        user_prompt += f"\n\n[Input truncated to {TRIAL_CODE_CHAR_LIMIT} characters — trial mode]"

    try:
        result, input_tokens, output_tokens = _call_claude(
            TRIAL_REVIEW_SYSTEM_PROMPT, user_prompt
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "tier": "trial",
            "input_truncated": was_truncated,
            "original_char_count": len(body.code),
            "rate_limit_remaining": remaining,
            "upgrade_url": f"{SERVICE_URL}/.well-known/l402.json",
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_trial("/trial/review", "success", ip=ip,
                  duration_ms=duration_ms, input_tokens=input_tokens,
                  output_tokens=output_tokens, char_count=len(code))
        return JSONResponse(content=result)

    except HTTPException as e:
        log_trial("/trial/review", "error", ip=ip, error=e.detail,
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_trial("/trial/review", "error", ip=ip, error=str(e),
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/summarise")
async def trial_summarise(body: TrialTextRequest, request: Request):
    """
    Free trial text summary — real Claude response, capped at 1000 chars input.

    Rate limited: {RATE_LIMIT_CALLS} calls per IP per hour.
    For full PDF summarisation, use POST /summarise/url (500 sats).

    Request body:
        text (str, required) - text to summarise (max 1000 chars)
    """
    t0 = time.monotonic()
    ip = _client_ip(request)

    allowed, remaining = _check_rate_limit(ip)
    if not allowed:
        log_trial("/trial/summarise", "rate_limited", ip=ip,
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Trial rate limit reached ({RATE_LIMIT_CALLS} calls/hour). "
                "Upgrade to paid via L402: "
                f"{SERVICE_URL}/.well-known/l402.json"
            ),
        )

    text = body.text[:TRIAL_TEXT_CHAR_LIMIT]
    was_truncated = len(body.text) > TRIAL_TEXT_CHAR_LIMIT

    user_prompt = f"Summarise this text:\n\n{text}"
    if was_truncated:
        user_prompt += f"\n\n[Input truncated to {TRIAL_TEXT_CHAR_LIMIT} characters — trial mode]"

    try:
        result, input_tokens, output_tokens = _call_claude(
            TRIAL_SUMMARISE_SYSTEM_PROMPT, user_prompt
        )
        result["_meta"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": "claude-sonnet-4-6",
            "tier": "trial",
            "input_truncated": was_truncated,
            "original_char_count": len(body.text),
            "rate_limit_remaining": remaining,
            "upgrade_url": f"{SERVICE_URL}/.well-known/l402.json",
        }
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_trial("/trial/summarise", "success", ip=ip,
                  duration_ms=duration_ms, input_tokens=input_tokens,
                  output_tokens=output_tokens, char_count=len(text))
        return JSONResponse(content=result)

    except HTTPException as e:
        log_trial("/trial/summarise", "error", ip=ip, error=e.detail,
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    except Exception as e:
        log_trial("/trial/summarise", "error", ip=ip, error=str(e),
                  duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
