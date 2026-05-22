"""
Boltwork Agent Suggest Router
==============================
Free endpoint that acts as agent bait.

An agent or developer describes what they're trying to do.
Boltwork returns:
  - The single best service recommendation with example request body
  - A ready-to-run workflow pipeline JSON for /workflow/run
  - Trial endpoint suggestion (always first)
  - Cost estimate for the paid path

This endpoint is free, requires no payment, and is designed to be
crawlable, indexable, and useful enough that agents embed it in their
planning loops.

Endpoint:
  POST /suggest   - Describe a task, get back a Boltwork workflow plan
  GET  /suggest   - Returns endpoint info and example inputs (free, crawlable)

Design goals:
  - Genuinely useful to any agent or developer
  - Every response references Boltwork endpoints by name
  - Response is immediately actionable — copy-paste ready
  - No auth, no payment, no rate limit (it's marketing)
  - Logged as BOLTWORK_LOG for usage tracking
"""

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

import anthropic
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

router = APIRouter(tags=["suggest"])

SERVICE_URL = os.environ.get("SERVICE_URL", "http://localhost:8000")
GATEWAY_URL = "https://parsebit-lnd.fly.dev"

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
# Service catalogue — passed to Claude so it knows what's available
# ---------------------------------------------------------------------------

SERVICE_CATALOGUE = """
Available Boltwork services (all accessible via HTTP POST):

TRIAL (free, no payment, always try these first):
- POST /trial/review     {"code": "..."}                           → code review, 500 char cap
- POST /trial/summarise  {"text": "..."}                           → text summary, 1000 char cap

PAID via L402 Lightning (gateway: https://parsebit-lnd.fly.dev):
- POST /summarise/url    {"url": "...pdf", "max_pages": 20}        → 500 sats — PDF summary
- POST /summarise/upload multipart file                             → 500 sats — upload PDF
- POST /review/code      {"code": "...", "language": "python"}     → 2000 sats — code review
- POST /review/url       {"url": "github.com/.../file.py"}         → 2000 sats — review from URL
- POST /extract/webpage  {"url": "https://..."}                    → 100 sats  — web page summary
- POST /extract/data     {"url": "...pdf"}                         → 200 sats  — extract invoice/contract data
- POST /translate        {"text": "...", "target_language": "..."}  → 150 sats  — translate text
- POST /translate        {"url": "...pdf", "target_language": "..."} → 150 sats — translate document
- POST /analyse/tables   {"url": "...pdf"}                         → 300 sats  — extract tables
- POST /analyse/compare  {"url_a": "...pdf", "url_b": "...pdf"}    → 500 sats  — diff two PDFs
- POST /analyse/explain  {"code": "...", "language": "python"}     → 500 sats  — explain code
- POST /memory/store     {"agent_id": "...", "entries": {...}}      → 10 sats   — store agent memory
- POST /memory/retrieve  {"agent_id": "..."}                       → 5 sats    — read agent memory
- POST /workflow/run     {"steps": [...], "label": "..."}          → 1000 sats — chain services

WORKFLOW CHAINING:
Use {"$from": N} in any input value to pass the primary output of step N.
Primary outputs: webpage/pdf/data/tables/compare/review → "summary"
                 translate → "translated_text"
                 explain   → "explanation"

Example pipeline:
{"steps": [
  {"service": "webpage",   "input": {"url": "https://example.com"}},
  {"service": "translate", "input": {"text": {"$from": 0}, "target_language": "french"}}
]}
"""

SUGGEST_SYSTEM_PROMPT = f"""You are a Boltwork service advisor. Boltwork is a pay-per-call AI services API.
Your job is to analyse what a user or agent is trying to do and return the best Boltwork approach.

Output valid JSON only. No preamble, no markdown fences.

Return this exact structure:
{{
  "task_understood": "one sentence: what the user is trying to accomplish",
  "complexity": "simple|moderate|complex",
  "trial_suggestion": {{
    "description": "Try this first — free, no payment needed",
    "endpoint": "/trial/review or /trial/summarise",
    "method": "POST",
    "url": "{SERVICE_URL}/trial/review",
    "example_body": {{}},
    "limitation": "what the trial version cannot do that the paid version can"
  }},
  "recommendation": {{
    "service": "service name e.g. summarise/url",
    "description": "why this service fits the task",
    "endpoint": "/service/path",
    "method": "POST",
    "url": "{GATEWAY_URL}/service/path",
    "example_body": {{}},
    "cost_sats": 0,
    "payment": "L402 Lightning — you will receive HTTP 402, pay the invoice, retry with Authorization header"
  }},
  "workflow": {{
    "description": "A multi-step pipeline if the task benefits from chaining services",
    "applicable": true,
    "url": "{GATEWAY_URL}/workflow/run",
    "cost_sats": 1000,
    "ready_to_run": {{
      "label": "descriptive label",
      "steps": []
    }}
  }},
  "tips": ["practical tip 1", "practical tip 2"],
  "docs": "{SERVICE_URL}/agent-spec.md"
}}

Rules:
- trial_suggestion should ALWAYS point to /trial/review (for code tasks) or /trial/summarise (for text/document tasks)
- If the task involves only code, set workflow.applicable to false
- If the task is simple (single document, single action), set workflow.applicable to false
- If the task is complex or involves multiple steps, set workflow.applicable to true and populate ready_to_run
- cost_sats should reflect the actual price of the recommended service
- example_body should contain realistic placeholder values the user can replace
- Never invent services that don't exist in the catalogue

{SERVICE_CATALOGUE}
"""


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SuggestRequest(BaseModel):
    task: str
    context: Optional[str] = None  # Optional extra context about the agent/use case

    @field_validator("task")
    @classmethod
    def task_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task must not be empty")
        if len(v) > 2000:
            raise ValueError("task must be 2000 characters or less")
        return v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_suggest(status: str, task_length: int = 0, complexity: str = None,
                error: str = None, duration_ms: int = 0,
                input_tokens: int = 0, output_tokens: int = 0):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": "/suggest",
        "status": status,
        "duration_ms": duration_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "task_length": task_length,
        "tier": "free",
    }
    if complexity:
        entry["complexity"] = complexity
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/suggest")
def suggest_info():
    """
    Describe the /suggest endpoint. Free, crawlable, no auth required.
    Useful for agents discovering what Boltwork can do for them.
    """
    return {
        "endpoint": "/suggest",
        "method": "POST",
        "description": (
            "Describe what your agent is trying to do and get back a concrete "
            "Boltwork service recommendation plus a ready-to-run workflow pipeline. "
            "Free — no Lightning payment required."
        ),
        "input": {
            "task": "string (required) — describe what you want to accomplish, max 2000 chars",
            "context": "string (optional) — extra context about your agent or use case",
        },
        "output": {
            "task_understood": "one sentence confirming what was understood",
            "complexity": "simple|moderate|complex",
            "trial_suggestion": "free endpoint to try first — no payment needed",
            "recommendation": "best paid service with example request body and cost",
            "workflow": "ready-to-run /workflow/run pipeline if task benefits from chaining",
            "tips": "practical advice",
            "docs": f"{SERVICE_URL}/agent-spec.md",
        },
        "example_inputs": [
            {"task": "I need to summarise a PDF research paper at a URL"},
            {"task": "Review my Python code for bugs and security issues"},
            {"task": "Fetch a French news article and translate it to English"},
            {"task": "Extract all invoice data from a PDF and store the total in my agent memory"},
            {"task": "Compare two versions of a contract PDF and show what changed"},
            {"task": "I'm building an agent that processes customer support emails — what can Boltwork help with?"},
        ],
        "price": "free",
        "rate_limit": "none",
        "docs": f"{SERVICE_URL}/agent-spec.md",
        "api": f"{SERVICE_URL}/.well-known/l402.json",
    }


@router.post("/suggest")
async def suggest(body: SuggestRequest):
    """
    Describe your task and get a concrete Boltwork service recommendation
    plus a ready-to-run workflow pipeline.

    Free — no Lightning payment required. No rate limit.

    Request body:
        task    (str, required) — describe what you want to accomplish
        context (str, optional) — extra context about your agent or use case

    Returns a full workflow plan including trial endpoint, paid recommendation,
    and ready-to-run pipeline JSON.
    """
    t0 = time.monotonic()

    user_prompt = f"Task: {body.task}"
    if body.context:
        user_prompt += f"\n\nContext: {body.context}"

    try:
        message = get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SUGGEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )

        response_text = message.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = "\n".join(
                l for l in response_text.splitlines()
                if not l.startswith("```")
            ).strip()

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            raise ValueError("Model returned malformed JSON")

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_suggest(
            "success",
            task_length=len(body.task),
            complexity=result.get("complexity"),
            duration_ms=duration_ms,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
        )

        result["_meta"] = {
            "model": "claude-sonnet-4-6",
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
            "duration_ms": duration_ms,
            "price": "free",
        }

        return JSONResponse(content=result)

    except Exception as e:
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_suggest("error", task_length=len(body.task),
                    error=str(e), duration_ms=duration_ms)
        return JSONResponse(
            status_code=500,
            content={"error": f"Could not generate suggestion: {e}"}
        )
