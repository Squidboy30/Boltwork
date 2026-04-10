"""
Boltwork Memory Router
========================
Persistent key-value memory store for agents.
Agents can store and retrieve structured context across sessions — e.g.
"last reviewed file", "preferred output language", "known issues in repo X".

Endpoints:
  POST /memory/store      - Write one or more key-value pairs (10 sats/write)
  POST /memory/retrieve   - Read keys by agent_id (5 sats/read)
  POST /memory/delete     - Delete a specific key (free — no sense charging)
  GET  /memory/info       - Describe the memory service (free)

Storage: SQLite via stdlib sqlite3 — no extra deps.
Schema:
  table: agent_memory
    agent_id  TEXT   — caller-provided stable agent identifier
    key       TEXT   — namespaced key (max 128 chars)
    value     TEXT   — JSON-serialisable value (max 4096 chars)
    updated   TEXT   — ISO8601 UTC timestamp of last write

Constraints:
  - Max 100 keys per agent_id
  - Key max 128 chars, value max 4096 chars
  - agent_id max 128 chars
  - Values must be valid JSON (string, number, object, array, bool, null)

Pricing:
  - Write (store): 10 sats per call (any number of keys, up to 10 per call)
  - Read (retrieve): 5 sats per call (returns all matching keys, or specific ones)
  - Delete: free
  - Info: free

The agent_id is caller-supplied — no auth. This is intentional: agents own
their IDs and the data is not sensitive. Add auth at the Aperture/L402 layer
if you want access control.
"""

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/memory", tags=["memory"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_PATH         = os.environ.get("MEMORY_DB_PATH", "/tmp/boltwork_memory.db")
MAX_AGENT_KEYS  = 100
MAX_KEY_LEN     = 128
MAX_VALUE_LEN   = 4096
MAX_AGENT_ID_LEN = 128
MAX_KEYS_PER_WRITE = 10
SERVICE_URL     = os.environ.get("SERVICE_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# SQLite setup — thread-safe with a module-level lock + check_same_thread=False
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()
_db_conn: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _db_conn.row_factory = sqlite3.Row
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory (
                agent_id TEXT NOT NULL,
                key      TEXT NOT NULL,
                value    TEXT NOT NULL,
                updated  TEXT NOT NULL,
                PRIMARY KEY (agent_id, key)
            )
        """)
        _db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent ON agent_memory (agent_id)"
        )
        _db_conn.commit()
    return _db_conn


@contextmanager
def _db():
    """Thread-safe DB context manager. Commits on exit, rolls back on error."""
    conn = _get_conn()
    with _db_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class StoreRequest(BaseModel):
    agent_id: str
    entries: dict[str, Any]     # {key: value, ...}  max 10 keys per call

    @field_validator("agent_id")
    @classmethod
    def agent_id_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_id must not be empty")
        if len(v) > MAX_AGENT_ID_LEN:
            raise ValueError(f"agent_id max {MAX_AGENT_ID_LEN} characters")
        return v

    @field_validator("entries")
    @classmethod
    def entries_not_empty(cls, v: dict) -> dict:
        if not v:
            raise ValueError("entries must not be empty")
        if len(v) > MAX_KEYS_PER_WRITE:
            raise ValueError(f"Max {MAX_KEYS_PER_WRITE} keys per write call")
        return v


class RetrieveRequest(BaseModel):
    agent_id: str
    keys: Optional[list[str]] = None   # None = return all keys for agent

    @field_validator("agent_id")
    @classmethod
    def agent_id_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_id must not be empty")
        return v


class DeleteRequest(BaseModel):
    agent_id: str
    key: str

    @field_validator("agent_id")
    @classmethod
    def agent_id_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("agent_id must not be empty")
        return v

    @field_validator("key")
    @classmethod
    def key_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("key must not be empty")
        return v


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_memory(endpoint: str, status: str, agent_id: str = None,
               error: str = None, duration_ms: int = 0,
               keys_affected: int = 0):
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint,
        "status": status,
        "duration_ms": duration_ms,
        "keys_affected": keys_affected,
    }
    # Hash agent_id in logs to avoid leaking agent identifiers
    if agent_id:
        import hashlib
        entry["agent_id_hash"] = hashlib.sha256(agent_id.encode()).hexdigest()[:12]
    if error:
        entry["error"] = error
    print("BOLTWORK_LOG " + json.dumps(entry), flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_key(key: str):
    if len(key) > MAX_KEY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Key '{key[:32]}...' exceeds {MAX_KEY_LEN} char limit"
        )
    if not key.strip():
        raise HTTPException(status_code=400, detail="Keys must not be empty strings")


def _validate_value(key: str, value: Any) -> str:
    """Serialise value to JSON string and enforce size limit."""
    try:
        serialised = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Value for key '{key}' is not JSON-serialisable: {e}"
        )
    if len(serialised) > MAX_VALUE_LEN:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Value for key '{key}' is {len(serialised)} chars — "
                f"exceeds {MAX_VALUE_LEN} char limit"
            ),
        )
    return serialised


def _count_agent_keys(conn: sqlite3.Connection, agent_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/info")
def memory_info():
    """
    Describe the memory service, limits, and pricing. Free.
    """
    return {
        "service": "Boltwork Agent Memory",
        "description": (
            "Persistent key-value store for AI agents. "
            "Store structured context across sessions — no accounts needed. "
            "Keyed by your agent_id, paid per call via L402."
        ),
        "endpoints": {
            "POST /memory/store":    "Write key-value pairs — 10 sats per call",
            "POST /memory/retrieve": "Read keys by agent_id — 5 sats per call",
            "POST /memory/delete":   "Delete a key — free",
            "GET  /memory/info":     "This document — free",
        },
        "limits": {
            "max_keys_per_agent":  MAX_AGENT_KEYS,
            "max_key_length":      MAX_KEY_LEN,
            "max_value_length":    MAX_VALUE_LEN,
            "max_agent_id_length": MAX_AGENT_ID_LEN,
            "max_keys_per_write":  MAX_KEYS_PER_WRITE,
        },
        "value_format": "Any JSON-serialisable value: string, number, object, array, bool, null",
        "storage": "SQLite on Fly.io London region",
        "privacy": "agent_id is caller-supplied; no authentication enforced at this layer",
        "l402_manifest": f"{SERVICE_URL}/.well-known/l402.json",
    }


@router.post("/store")
async def memory_store(body: StoreRequest):
    """
    Write one or more key-value pairs for an agent.

    Request body:
        agent_id (str, required) - stable identifier for your agent
        entries  (dict, required) - {key: value, ...} — max 10 keys per call
                                    Values must be JSON-serialisable.

    Returns a summary of what was written.
    Price: 10 sats via L402.
    """
    t0 = time.monotonic()

    # Validate all keys + values before touching the DB
    serialised: dict[str, str] = {}
    for key, value in body.entries.items():
        _validate_key(key)
        serialised[key] = _validate_value(key, value)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with _db() as conn:
            # Check quota — count existing keys not already being overwritten
            existing_keys = {
                row[0] for row in conn.execute(
                    "SELECT key FROM agent_memory WHERE agent_id = ?",
                    (body.agent_id,)
                ).fetchall()
            }
            new_keys = set(serialised.keys()) - existing_keys
            current_count = len(existing_keys)

            if current_count + len(new_keys) > MAX_AGENT_KEYS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Agent memory quota exceeded. "
                        f"Currently {current_count}/{MAX_AGENT_KEYS} keys. "
                        f"Delete old keys before writing {len(new_keys)} new ones."
                    ),
                )

            for key, value_json in serialised.items():
                conn.execute(
                    """INSERT INTO agent_memory (agent_id, key, value, updated)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(agent_id, key) DO UPDATE SET
                         value   = excluded.value,
                         updated = excluded.updated""",
                    (body.agent_id, key, value_json, now_str),
                )

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_memory("/memory/store", "success", agent_id=body.agent_id,
                   duration_ms=duration_ms, keys_affected=len(serialised))

        return JSONResponse(content={
            "status": "ok",
            "agent_id": body.agent_id,
            "keys_written": list(serialised.keys()),
            "written_at": now_str,
            "keys_in_store": current_count + len(new_keys),
            "quota_remaining": MAX_AGENT_KEYS - (current_count + len(new_keys)),
            "_meta": {"tier": "paid", "price_sats": 10},
        })

    except HTTPException:
        raise
    except Exception as e:
        log_memory("/memory/store", "error", agent_id=body.agent_id,
                   error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Storage error: {e}")


@router.post("/retrieve")
async def memory_retrieve(body: RetrieveRequest):
    """
    Read stored keys for an agent.

    Request body:
        agent_id (str, required)          - agent identifier
        keys     (list[str], optional)    - specific keys to fetch.
                                            Omit to return all keys.

    Returns a dict of {key: value} for all found keys.
    Price: 5 sats via L402.
    """
    t0 = time.monotonic()

    try:
        with _db() as conn:
            if body.keys:
                placeholders = ",".join("?" * len(body.keys))
                rows = conn.execute(
                    f"SELECT key, value, updated FROM agent_memory "
                    f"WHERE agent_id = ? AND key IN ({placeholders})",
                    (body.agent_id, *body.keys),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, value, updated FROM agent_memory "
                    "WHERE agent_id = ? ORDER BY key",
                    (body.agent_id,),
                ).fetchall()

        result: dict[str, Any] = {}
        timestamps: dict[str, str] = {}
        for row in rows:
            key = row["key"]
            result[key] = json.loads(row["value"])
            timestamps[key] = row["updated"]

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_memory("/memory/retrieve", "success", agent_id=body.agent_id,
                   duration_ms=duration_ms, keys_affected=len(result))

        return JSONResponse(content={
            "agent_id": body.agent_id,
            "entries": result,
            "updated_at": timestamps,
            "keys_found": len(result),
            "keys_requested": len(body.keys) if body.keys else None,
            "_meta": {"tier": "paid", "price_sats": 5},
        })

    except HTTPException:
        raise
    except Exception as e:
        log_memory("/memory/retrieve", "error", agent_id=body.agent_id,
                   error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Retrieval error: {e}")


@router.post("/delete")
async def memory_delete(body: DeleteRequest):
    """
    Delete a specific key from an agent's memory store. Free.

    Request body:
        agent_id (str, required) - agent identifier
        key      (str, required) - key to delete
    """
    t0 = time.monotonic()

    try:
        with _db() as conn:
            cursor = conn.execute(
                "DELETE FROM agent_memory WHERE agent_id = ? AND key = ?",
                (body.agent_id, body.key),
            )
            deleted = cursor.rowcount

        duration_ms = int((time.monotonic() - t0) * 1000)
        log_memory("/memory/delete", "success", agent_id=body.agent_id,
                   duration_ms=duration_ms, keys_affected=deleted)

        return JSONResponse(content={
            "status": "ok",
            "agent_id": body.agent_id,
            "key": body.key,
            "deleted": deleted > 0,
            "_meta": {"tier": "free", "price_sats": 0},
        })

    except Exception as e:
        log_memory("/memory/delete", "error", agent_id=body.agent_id,
                   error=str(e), duration_ms=int((time.monotonic() - t0) * 1000))
        raise HTTPException(status_code=500, detail=f"Delete error: {e}")
