"""
Boltwork MCP Server
====================
Exposes all Boltwork AI services as MCP tools.
Handles L402 Lightning payments transparently.

Usage:
  pip install boltwork-mcp
  
  Then add to claude_desktop_config.json or .cursor/mcp.json:
  {
    "mcpServers": {
      "boltwork": {
        "command": "uvx",
        "args": ["boltwork-mcp"],
        "env": {
          "NWC_CONNECTION_STRING": "nostr+walletconnect://..."
        }
      }
    }
  }
"""

import os
import asyncio
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

from boltwork_mcp.payment import l402_request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GATEWAY = os.environ.get("BOLTWORK_GATEWAY", "https://parsebit-lnd.fly.dev")

# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

app = Server("boltwork")


@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [

        # ── Summarisation ────────────────────────────────────────────────
        types.Tool(
            name="summarise_pdf",
            description=(
                "Summarise a PDF document from a URL. Returns a structured summary "
                "including title, key points, sentiment, topics, and word count. "
                "Costs 500 sats via Lightning. "
                "Use for: research papers, reports, contracts, any PDF."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "URL of the PDF to summarise"},
                    "max_pages": {"type": "integer", "description": "Max pages to process (default 20)", "default": 20},
                },
                "required": ["url"],
            },
        ),

        # ── Code Review ──────────────────────────────────────────────────
        types.Tool(
            name="review_code",
            description=(
                "Review source code and return a structured analysis covering bugs, "
                "security issues, code quality, strengths, and recommended actions. "
                "Returns an overall score 1-10. "
                "Costs 2000 sats via Lightning. "
                "Supports: Python, JavaScript, TypeScript, Go, Rust, Java, C/C++, "
                "C#, Ruby, PHP, Swift, Kotlin, Scala, Shell, SQL, Terraform, and more."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code":     {"type": "string", "description": "The source code to review"},
                    "language": {"type": "string", "description": "Language hint (optional, auto-detected if omitted)"},
                    "filename": {"type": "string", "description": "Filename hint for language detection (optional)"},
                },
                "required": ["code"],
            },
        ),

        types.Tool(
            name="review_code_url",
            description=(
                "Review code fetched from a URL. Supports GitHub and GitLab blob URLs "
                "(auto-converted to raw). "
                "Costs 2000 sats via Lightning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url":      {"type": "string", "description": "URL to the code file (GitHub/GitLab/raw)"},
                    "language": {"type": "string", "description": "Language hint (optional)"},
                },
                "required": ["url"],
            },
        ),

        # ── Web Extraction ───────────────────────────────────────────────
        types.Tool(
            name="summarise_webpage",
            description=(
                "Summarise any web page by URL. Returns title, summary, key points, "
                "content type, sentiment, and topics. "
                "Costs 100 sats via Lightning. "
                "Use for: articles, blogs, documentation, product pages, news."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL of the web page to summarise"},
                },
                "required": ["url"],
            },
        ),

        # ── Data Extraction ──────────────────────────────────────────────
        types.Tool(
            name="extract_data",
            description=(
                "Extract structured data from a PDF document. Returns document type, "
                "dates, parties, amounts, line items, and reference numbers. "
                "Costs 200 sats via Lightning. "
                "Use for: invoices, contracts, receipts, forms."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "URL of the PDF"},
                    "max_pages": {"type": "integer", "description": "Max pages to process (default 20)", "default": 20},
                },
                "required": ["url"],
            },
        ),

        # ── Translation ──────────────────────────────────────────────────
        types.Tool(
            name="translate",
            description=(
                "Translate text or a document URL to any of 24 supported languages. "
                "Detects source language automatically. "
                "Costs 150 sats via Lightning. "
                "Supported languages: Spanish, French, German, Italian, Portuguese, "
                "Dutch, Russian, Japanese, Chinese, Korean, Arabic, Hindi, Turkish, "
                "Polish, Swedish, Danish, Norwegian, Finnish, Czech, Romanian, "
                "Hungarian, Greek, Hebrew, Thai."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text":            {"type": "string", "description": "Text to translate (provide either text or url)"},
                    "url":             {"type": "string", "description": "URL of document to translate (provide either text or url)"},
                    "target_language": {"type": "string", "description": "Target language (e.g. 'spanish', 'french', 'japanese')"},
                },
                "required": ["target_language"],
            },
        ),

        # ── Analysis ─────────────────────────────────────────────────────
        types.Tool(
            name="extract_tables",
            description=(
                "Extract all tables from a PDF as structured JSON. "
                "Returns table count, headers, rows, and a summary. "
                "Costs 300 sats via Lightning. "
                "Use for: financial reports, research data, invoices with line items."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url":       {"type": "string",  "description": "URL of the PDF"},
                    "max_pages": {"type": "integer", "description": "Max pages to process (default 20)", "default": 20},
                },
                "required": ["url"],
            },
        ),

        types.Tool(
            name="compare_documents",
            description=(
                "Compare two PDF documents and return a structured diff. "
                "Identifies additions, removals, modifications, and overall similarity. "
                "Costs 500 sats via Lightning. "
                "Use for: contract versions, policy updates, paper revisions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url_a":     {"type": "string",  "description": "URL of the first PDF (original)"},
                    "url_b":     {"type": "string",  "description": "URL of the second PDF (revised)"},
                    "max_pages": {"type": "integer", "description": "Max pages per document (default 20)", "default": 20},
                },
                "required": ["url_a", "url_b"],
            },
        ),

        types.Tool(
            name="explain_code",
            description=(
                "Explain what code does in plain English. Unlike code review (which finds "
                "problems), this explains purpose and behaviour to a non-programmer. "
                "Costs 500 sats via Lightning. "
                "Use for: understanding inherited code, due diligence, onboarding docs."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "code":     {"type": "string", "description": "Source code to explain (provide either code or url)"},
                    "url":      {"type": "string", "description": "URL of code file to explain (provide either code or url)"},
                    "language": {"type": "string", "description": "Language hint (optional)"},
                },
            },
        ),

        # ── Agent Memory ─────────────────────────────────────────────────
        types.Tool(
            name="memory_store",
            description=(
                "Store persistent key-value memory for your agent. "
                "Data persists across sessions, keyed by agent_id. "
                "Up to 100 keys per agent, 10 keys per write call. "
                "Costs 10 sats via Lightning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Stable identifier for your agent"},
                    "entries":  {"type": "object", "description": "Key-value pairs to store (max 10 keys, values must be JSON-serialisable)"},
                },
                "required": ["agent_id", "entries"],
            },
        ),

        types.Tool(
            name="memory_retrieve",
            description=(
                "Retrieve stored memory for your agent. "
                "Returns all keys or a specific subset. "
                "Costs 5 sats via Lightning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string",       "description": "Agent identifier"},
                    "keys":     {"type": "array",        "description": "Specific keys to fetch (omit to return all)", "items": {"type": "string"}},
                },
                "required": ["agent_id"],
            },
        ),

        types.Tool(
            name="memory_delete",
            description="Delete a single key from your agent's memory store. Free.",
            inputSchema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Agent identifier"},
                    "key":      {"type": "string", "description": "Key to delete"},
                },
                "required": ["agent_id", "key"],
            },
        ),

        # ── Workflow Pipelines ────────────────────────────────────────────
        types.Tool(
            name="run_workflow",
            description=(
                "Chain multiple Boltwork services in a single call. "
                "Pay once, describe a pipeline of up to 5 steps, get the final result "
                "plus all intermediate outputs. "
                "Use {\"$from\": N} in any input value to pass the primary output of "
                "step N into the current step. "
                "Costs 1000 sats via Lightning. "
                "Supported services: webpage, pdf, summarise, translate, data, "
                "tables, explain, review, compare. "
                "Example: fetch a webpage, translate the summary to French — "
                "steps: [{service: webpage, input: {url: ...}}, "
                "{service: translate, input: {text: {$from: 0}, target_language: french}}]"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "description": "Pipeline steps, each with 'service' and 'input'",
                        "items": {
                            "type": "object",
                            "properties": {
                                "service": {"type": "string"},
                                "input":   {"type": "object"},
                            },
                            "required": ["service", "input"],
                        },
                        "maxItems": 5,
                    },
                    "label": {"type": "string", "description": "Optional label for this pipeline"},
                },
                "required": ["steps"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool call handlers
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    try:
        result = await _dispatch(name, arguments)
        import json
        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


async def _dispatch(name: str, args: dict) -> dict:
    match name:

        case "summarise_pdf":
            return await l402_request(
                "POST", "/summarise/url",
                f"{GATEWAY}/summarise/url",
                json_body={"url": args["url"], "max_pages": args.get("max_pages", 20)},
            )

        case "review_code":
            return await l402_request(
                "POST", "/review/code",
                f"{GATEWAY}/review/code",
                json_body={
                    "code":     args["code"],
                    "language": args.get("language"),
                    "filename": args.get("filename"),
                },
            )

        case "review_code_url":
            return await l402_request(
                "POST", "/review/url",
                f"{GATEWAY}/review/url",
                json_body={"url": args["url"], "language": args.get("language")},
            )

        case "summarise_webpage":
            return await l402_request(
                "POST", "/extract/webpage",
                f"{GATEWAY}/extract/webpage",
                json_body={"url": args["url"]},
            )

        case "extract_data":
            return await l402_request(
                "POST", "/extract/data",
                f"{GATEWAY}/extract/data",
                json_body={"url": args["url"], "max_pages": args.get("max_pages", 20)},
            )

        case "translate":
            body = {"target_language": args["target_language"]}
            if "text" in args:
                body["text"] = args["text"]
            if "url" in args:
                body["url"] = args["url"]
            return await l402_request(
                "POST", "/translate",
                f"{GATEWAY}/translate",
                json_body=body,
            )

        case "extract_tables":
            return await l402_request(
                "POST", "/analyse/tables",
                f"{GATEWAY}/analyse/tables",
                json_body={"url": args["url"], "max_pages": args.get("max_pages", 20)},
            )

        case "compare_documents":
            return await l402_request(
                "POST", "/analyse/compare",
                f"{GATEWAY}/analyse/compare",
                json_body={
                    "url_a":     args["url_a"],
                    "url_b":     args["url_b"],
                    "max_pages": args.get("max_pages", 20),
                },
            )

        case "explain_code":
            body = {}
            if "code" in args:
                body["code"] = args["code"]
            if "url" in args:
                body["url"] = args["url"]
            if "language" in args:
                body["language"] = args["language"]
            return await l402_request(
                "POST", "/analyse/explain",
                f"{GATEWAY}/analyse/explain",
                json_body=body,
            )

        case "memory_store":
            return await l402_request(
                "POST", "/memory/store",
                f"{GATEWAY}/memory/store",
                json_body={"agent_id": args["agent_id"], "entries": args["entries"]},
            )

        case "memory_retrieve":
            body = {"agent_id": args["agent_id"]}
            if "keys" in args:
                body["keys"] = args["keys"]
            return await l402_request(
                "POST", "/memory/retrieve",
                f"{GATEWAY}/memory/retrieve",
                json_body=body,
            )

        case "memory_delete":
            return await l402_request(
                "POST", "/memory/delete",
                f"{GATEWAY}/memory/delete",
                json_body={"agent_id": args["agent_id"], "key": args["key"]},
            )

        case "run_workflow":
            return await l402_request(
                "POST", "/workflow/run",
                f"{GATEWAY}/workflow/run",
                json_body={"steps": args["steps"], "label": args.get("label")},
            )

        case _:
            raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options(),
        )


def run():
    asyncio.run(main())


if __name__ == "__main__":
    run()
