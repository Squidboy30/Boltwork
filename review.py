"""
Parsebit Phase 2 - Code Review Router
======================================
Isolated module. Zero shared state with summarise endpoints.
All review logic lives here. main.py includes this with one line.

Endpoints:
  POST /review/code  - Review code submitted as text
  POST /review/url   - Review code fetched from a URL (GitHub, GitLab, raw URL)

Price: 2000 sats per review (set in Aperture config)
"""

import json
import os
import re
import socket
import time
from typing import Optional

import anthropic
import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/review", tags=["code-review"])

# ---------------------------------------------------------------------------
# Anthropic client (reuses env var already set by main.py)
# ---------------------------------------------------------------------------

_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    """Lazy singleton — safe even if main.py already created one."""
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

MAX_CODE_CHARS = 80_000   # ~20k tokens of code
MAX_URL_BYTES  = 2 * 1024 * 1024  # 2MB max for a code file

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go",
    ".rs", ".c", ".cpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".sql", ".tf",
    ".yaml", ".yml", ".json", ".toml",
}

LANGUAGE_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "javascript", ".tsx": "typescript", ".java": "java",
    ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "c++",
    ".cs": "c#", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".sh": "shell",
    ".bash": "shell", ".sql": "sql", ".tf": "terraform",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
}

REVIEW_SYSTEM_PROMPT = """You are an expert code reviewer with deep knowledge across all major programming languages and frameworks.

Your output is ALWAYS valid JSON - nothing else, no preamble, no markdown fences.

Return this EXACT structure:
{
  "language": "detected language name",
  "overall_score": 7,
  "summary": "2-3 sentence overall assessment",
  "bugs": [
    {
      "severity": "critical|high|medium|low",
      "line": 42,
      "description": "clear description of the bug",
      "suggestion": "how to fix it"
    }
  ],
  "security_issues": [
    {
      "severity": "critical|high|medium|low",
      "line": 10,
      "description": "description of the security issue",
      "suggestion": "how to fix it"
    }
  ],
  "code_quality": [
    {
      "category": "readability|maintainability|performance|style|testing",
      "description": "observation",
      "suggestion": "improvement"
    }
  ],
  "strengths": ["what the code does well"],
  "recommended_actions": ["prioritised list of what to fix first"],
  "_meta": {}
}

Rules:
- overall_score is 1-10 (10 = production perfect)
- bugs contains actual defects that would cause incorrect behaviour
- security_issues contains exploitable vulnerabilities (SQLi, XSS, path traversal, hardcoded secrets etc.)
- code_quality contains style/structure observations that aren't bugs
- strengths contains genuine positives — don't fabricate them if none exist
- recommended_actions is sorted: most critical first
- line numbers are best-effort; use null if not determinable
- Never include text outside the JSON object"""


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class CodeRequest(BaseModel):
    code: str
    language: Optional[str] = None  # Override auto-detection
    filename: Optional[str] = None  # Helps language detection

    @field_validator("code")
    @classmethod
    def code_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("code must not be empty")
        return v


class UrlReviewRequest(BaseModel):
    url: str
    language: Optional[str] = None  # Override auto-detection

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("url must not be empty")
        return v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log_review(endpoint: str, status: str, language: str = None,
               error: str = None, duration_ms: int = 0,
               char_count: int = 0, source_url: str = None,
               score: int = None):
    """Structured log line compatible with existing PARSEBIT_LOG format."""
    from datetime import datetime, timezone
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
    }
    if language:
        entry["language"] = language
    if char_count:
        entry["char_count"] = char_count
    if source_url:
        entry["source_url"] = source_url
    if score is not None:
        entry["overall_score"] = score
    if error:
        entry["error"] = error
    print("PARSEBIT_LOG " + json.dumps(entry), flush=True)


def detect_language(code: str, filename: Optional[str] = None,
                    hint: Optional[str] = None) -> str:
    """Best-effort language detection from filename extension or code patterns."""
    if hint:
        return hint.lower()

    if filename:
        for ext, lang in LANGUAGE_MAP.items():
            if filename.lower().endswith(ext):
                return lang

    # Heuristic patterns
    patterns = [
        (r"\bdef\s+\w+\s*\(", "python"),
        (r"\bimport\s+\w+", "python"),
        (r"^\s*package\s+main\b", "go"),
        (r"\bfunc\s+\w+\s*\(", "go"),
        (r"\bfn\s+\w+\s*\(", "rust"),
        (r"\bpub\s+fn\s+", "rust"),
        (r"\bpublic\s+class\s+\w+", "java"),
        (r"\bSystem\.out\.println", "java"),
        (r"\bconst\s+\w+\s*=\s*require\(", "javascript"),
        (r"\bmodule\.exports\s*=", "javascript"),
        (r"\bimport\s+.*\bfrom\b", "javascript"),
        (r":\s*\w+\s*[=;]", "typescript"),
        (r"\binterface\s+\w+\s*\{", "typescript"),
    ]
    for pattern, lang in patterns:
        if re.search(pattern, code, re.MULTILINE):
            return lang

    return "unknown"


def normalise_github_url(url: str) -> str:
    """Convert GitHub blob URLs to raw content URLs."""
    # https://github.com/user/repo/blob/main/file.py
    # -> https://raw.githubusercontent.com/user/repo/main/file.py
    match = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)", url
    )
    if match:
        user, repo, path = match.groups()
        return f"https://raw.githubusercontent.com/{user}/{repo}/{path}"

    # https://gitlab.com/user/repo/-/blob/main/file.py
    # -> https://gitlab.com/user/repo/-/raw/main/file.py
    match = re.match(
        r"(https://gitlab\.com/[^/]+/[^/]+)/-/blob/(.+)", url
    )
    if match:
        base, path = match.groups()
        return f"{base}/-/raw/{path}"

    return url


async def resolve_hostname_doh(hostname: str) -> str:
    """DNS-over-HTTPS — same approach as main.py to work on Fly.io."""
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


async def fetch_code_from_url(url: str) -> str:
    """Fetch raw code from a URL, handling GitHub/GitLab blob URLs."""
    from urllib.parse import urlparse, urlunparse

    raw_url = normalise_github_url(url)
    parsed = urlparse(raw_url)
    hostname = parsed.hostname

    try:
        ip = await resolve_hostname_doh(hostname)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        netloc = f"{ip}:{port}"
        ip_url = urlunparse((
            parsed.scheme, netloc, parsed.path,
            parsed.params, parsed.query, parsed.fragment
        ))
        async with httpx.AsyncClient(
            timeout=30.0, follow_redirects=True, verify=False
        ) as http:
            response = await http.get(ip_url, headers={"Host": hostname})
    except Exception:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            response = await http.get(raw_url)

    response.raise_for_status()

    if len(response.content) > MAX_URL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {MAX_URL_BYTES // 1024}KB for code review."
        )

    # Reject binary/non-text responses
    content_type = response.headers.get("content-type", "")
    if "text" not in content_type and "json" not in content_type and "application/octet" not in content_type:
        # Try to decode anyway
        pass

    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return response.content.decode("latin-1")
        except Exception:
            raise HTTPException(
                status_code=422,
                detail="Could not decode file as text. Is this a binary file?"
            )


def run_review(code: str, language: str) -> dict:
    """Send code to Claude for review. Returns structured JSON dict."""
    truncated = code[:MAX_CODE_CHARS]
    truncation_note = ""
    if len(code) > MAX_CODE_CHARS:
        truncation_note = f"\n\n[Code truncated at {MAX_CODE_CHARS} characters for review]"

    prompt = (
        f"Language: {language}\n\n"
        f"Review this code:\n\n```{language}\n{truncated}{truncation_note}\n```"
    )

    message = get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=REVIEW_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    # Strip any accidental markdown fences
    if response_text.startswith("```"):
        response_text = "\n".join(
            line for line in response_text.splitlines()
            if not line.startswith("```")
        ).strip()

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Model returned malformed JSON: {e}. Please retry."
        )

    # Ensure _meta is always present
    result["_meta"] = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
        "model": message.model,
        "truncated": len(code) > MAX_CODE_CHARS,
    }

    return result


def validate_review_result(result: dict) -> dict:
    """
    Defensive validation — ensure the response has expected structure.
    Fills in defaults for any missing fields rather than crashing.
    """
    defaults = {
        "language": "unknown",
        "overall_score": 5,
        "summary": "",
        "bugs": [],
        "security_issues": [],
        "code_quality": [],
        "strengths": [],
        "recommended_actions": [],
    }
    for key, default in defaults.items():
        if key not in result:
            result[key] = default

    # Clamp score to 1-10
    try:
        result["overall_score"] = max(1, min(10, int(result["overall_score"])))
    except (TypeError, ValueError):
        result["overall_score"] = 5

    # Ensure lists are actually lists
    for list_field in ["bugs", "security_issues", "code_quality", "strengths", "recommended_actions"]:
        if not isinstance(result[list_field], list):
            result[list_field] = []

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/code")
async def review_code(body: CodeRequest):
    """
    Review code submitted as plain text.

    Request body:
        code     (str, required) - the source code to review
        language (str, optional) - override language detection
        filename (str, optional) - helps language detection

    Returns structured JSON review.
    """
    t0 = time.monotonic()

    if len(body.code) > MAX_CODE_CHARS * 2:
        raise HTTPException(
            status_code=413,
            detail=f"Code too large. Max {MAX_CODE_CHARS * 2} characters."
        )

    language = detect_language(body.code, filename=body.filename, hint=body.language)

    try:
        result = run_review(body.code, language)
        result = validate_review_result(result)
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_review(
            "/review/code", "success",
            language=language,
            char_count=len(body.code),
            duration_ms=duration_ms,
            score=result.get("overall_score"),
        )
        return JSONResponse(content=result)

    except HTTPException as e:
        log_review(
            "/review/code", "error",
            error=e.detail,
            language=language,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise

    except Exception as e:
        log_review(
            "/review/code", "error",
            error=str(e),
            language=language,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


@router.post("/url")
async def review_url(body: UrlReviewRequest):
    """
    Review code fetched from a URL.

    Supports:
      - GitHub blob URLs (auto-converted to raw)
      - GitLab blob URLs (auto-converted to raw)
      - Any direct raw file URL

    Request body:
        url      (str, required) - URL to the code file
        language (str, optional) - override language detection

    Returns structured JSON review.
    """
    t0 = time.monotonic()
    language = "unknown"

    try:
        code = await fetch_code_from_url(str(body.url))
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        log_review(
            "/review/url", "error",
            error=f"HTTP {e.response.status_code}",
            source_url=str(body.url),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch URL: {e}"
        )
    except Exception as e:
        log_review(
            "/review/url", "error",
            error=str(e),
            source_url=str(body.url),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch URL: {e}"
        )

    # Detect language from URL path if possible
    from urllib.parse import urlparse
    path = urlparse(str(body.url)).path
    filename = path.split("/")[-1] if "/" in path else path
    language = detect_language(code, filename=filename, hint=body.language)

    try:
        result = run_review(code, language)
        result = validate_review_result(result)
        duration_ms = int((time.monotonic() - t0) * 1000)
        log_review(
            "/review/url", "success",
            language=language,
            char_count=len(code),
            source_url=str(body.url),
            duration_ms=duration_ms,
            score=result.get("overall_score"),
        )
        return JSONResponse(content=result)

    except HTTPException as e:
        log_review(
            "/review/url", "error",
            error=e.detail,
            language=language,
            source_url=str(body.url),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise

    except Exception as e:
        log_review(
            "/review/url", "error",
            error=str(e),
            language=language,
            source_url=str(body.url),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
