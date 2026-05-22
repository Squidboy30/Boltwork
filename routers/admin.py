"""
Boltwork Admin Dashboard Router
=================================
Provides live metrics for the admin dashboard.

Endpoints:
  GET /admin/metrics   — Live snapshot: LND balance, recent invoices, gateway health
  GET /admin/logs      — Recent Aperture log lines
  GET /admin/invoices  — Recent invoice activity
  GET /admin/lnd       — Live Lightning node stats: channels, balances, peers

Protected by ADMIN_TOKEN header.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
LND_HOST = os.environ.get("LND_HOST", "https://parsebit-lnd.fly.dev")
FLY_API_TOKEN = os.environ.get("FLY_API_TOKEN", "")
FLY_APP_LND = "parsebit-lnd"
FLY_APP_API = "parsebit"


def require_admin(x_admin_token: Optional[str] = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="Admin token not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


async def fetch_fly_logs(app_name: str, minutes: int = 30) -> list[dict]:
    """Fetch recent logs from Fly.io API."""
    if not FLY_API_TOKEN:
        return []
    try:
        url = f"https://api.fly.io/api/v1/apps/{app_name}/logs?limit=500"
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers={
                "Authorization": f"FlyV1 {FLY_API_TOKEN}",
                "Accept": "application/json"
            })
            if r.status_code != 200:
                return []
            data = r.json()
            logs = []
            for item in data.get("data", []):
                attrs = item.get("attributes", {})
                logs.append({
                    "message": attrs.get("message", ""),
                    "timestamp": attrs.get("timestamp", ""),
                })
            return logs
    except Exception:
        return []


async def get_gateway_health() -> dict:
    """Check if the L402 gateway is up and returning 402s."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            t0 = time.monotonic()
            r = await client.post(
                f"{LND_HOST}/extract/webpage",
                json={"url": "https://example.com"},
                timeout=8.0
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "status": "ok" if r.status_code == 402 else "degraded",
                "http_code": r.status_code,
                "latency_ms": latency_ms,
                "has_invoice": "invoice=" in r.headers.get("www-authenticate", ""),
            }
    except Exception as e:
        return {"status": "down", "error": str(e)[:80]}


async def get_parsebit_health() -> dict:
    """Check if the main API is up."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            t0 = time.monotonic()
            r = await client.get("https://parsebit.fly.dev/health")
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "status": "ok" if r.status_code == 200 else "degraded",
                "http_code": r.status_code,
                "latency_ms": latency_ms,
            }
    except Exception as e:
        return {"status": "down", "error": str(e)[:80]}


def parse_aperture_logs(logs: list[dict]) -> dict:
    """Extract metrics from Aperture log lines."""
    requests = []
    errors = []

    for entry in logs:
        msg = entry.get("message", "") or entry.get("text", "")
        if "PRXY:" not in msg:
            continue

        ts = entry.get("timestamp", entry.get("ts", ""))

        if '"POST' in msg or '"GET' in msg or '"HEAD' in msg or '"PUT' in msg:
            try:
                parts = msg.split('"')
                method_path = parts[1] if len(parts) > 1 else ""
                user_agent = parts[5] if len(parts) > 5 else ""
                method = method_path.split()[0] if method_path else ""
                path = method_path.split()[1] if len(method_path.split()) > 1 else ""
                requests.append({
                    "ts": ts,
                    "method": method,
                    "path": path,
                    "user_agent": user_agent,
                })
            except Exception:
                pass
        elif "Error" in msg or "error" in msg:
            errors.append({"ts": ts, "msg": msg[:120]})

    return {
        "total_requests": len(requests),
        "recent_requests": requests[-20:],
        "errors": errors[-10:],
        "unique_paths": list(set(r["path"] for r in requests if r.get("path"))),
    }


def parse_boltwork_logs(logs: list[dict]) -> dict:
    """Extract BOLTWORK_LOG entries from parsebit logs."""
    entries = []
    total_calls = 0
    success_calls = 0
    error_calls = 0
    by_endpoint = {}

    for entry in logs:
        msg = entry.get("message", "") or entry.get("text", "")
        if "BOLTWORK_LOG" not in msg:
            continue
        try:
            log_json = msg.split("BOLTWORK_LOG", 1)[1].strip()
            log = json.loads(log_json)
            entries.append(log)
            total_calls += 1
            status = log.get("status", "")
            if status == "success":
                success_calls += 1
            elif status == "error":
                error_calls += 1
            ep = log.get("endpoint", "unknown")
            if ep not in by_endpoint:
                by_endpoint[ep] = {"calls": 0, "success": 0, "errors": 0}
            by_endpoint[ep]["calls"] += 1
            if status == "success":
                by_endpoint[ep]["success"] += 1
            elif status == "error":
                by_endpoint[ep]["errors"] += 1
        except Exception:
            pass

    return {
        "total_calls": total_calls,
        "success_calls": success_calls,
        "error_calls": error_calls,
        "by_endpoint": by_endpoint,
        "recent": entries[-10:],
    }


@router.get("/metrics", dependencies=[Depends(require_admin)])
async def get_metrics():
    """
    Live dashboard metrics. Fetches in parallel:
    - Gateway health (402 check)
    - API health
    - Recent Fly logs (Aperture activity + Boltwork calls)
    - Invoice stats from gateway DB
    """
    gateway_health, api_health, lnd_logs, api_logs = await asyncio.gather(
        get_gateway_health(),
        get_parsebit_health(),
        fetch_fly_logs(FLY_APP_LND, minutes=60),
        fetch_fly_logs(FLY_APP_API, minutes=60),
        return_exceptions=True
    )

    if isinstance(gateway_health, Exception):
        gateway_health = {"status": "error", "error": str(gateway_health)}
    if isinstance(api_health, Exception):
        api_health = {"status": "error", "error": str(api_health)}
    if isinstance(lnd_logs, Exception):
        lnd_logs = []
    if isinstance(api_logs, Exception):
        api_logs = []

    aperture_stats = parse_aperture_logs(lnd_logs)
    boltwork_stats = parse_boltwork_logs(api_logs)

    invoice_stats = {"settled": 0, "pending": 0, "total_sats": 0}
    try:
        from pathlib import Path
        import sqlite3
        db_path = Path("/data/gateway.db")
        if db_path.exists():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT COUNT(*) as total, COALESCE(SUM(gross_sats),0) as sats FROM transactions"
            ).fetchone()
            invoice_stats["settled"] = row["total"]
            invoice_stats["total_sats"] = row["sats"]
            conn.close()
    except Exception:
        pass

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "health": {
            "gateway": gateway_health,
            "api": api_health,
        },
        "aperture": aperture_stats,
        "boltwork": boltwork_stats,
        "invoices": invoice_stats,
        "fly_logs_available": len(lnd_logs) > 0 or len(api_logs) > 0,
    }


@router.get("/health-simple")
async def health_simple():
    """Quick health check without auth — for monitoring."""
    gateway, api = await asyncio.gather(
        get_gateway_health(),
        get_parsebit_health(),
        return_exceptions=True
    )
    return {
        "gateway": gateway if not isinstance(gateway, Exception) else {"status": "error"},
        "api": api if not isinstance(api, Exception) else {"status": "error"},
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


@router.get("/lnd", dependencies=[Depends(require_admin)])
async def get_lnd_stats():
    """
    Live Lightning node stats: channels, balances, peers, sync status.
    Calls the LND REST API on parsebit-lnd.
    """
    LND_REST = os.environ.get("LND_REST_URL", "https://parsebit-lnd.fly.dev:8082")
    MACAROON  = os.environ.get("LND_MACAROON_HEX", "")

    if not MACAROON:
        return {
            "error": "LND_MACAROON_HEX not configured",
            "alias": "boltwork",
            "synced": None,
            "active_channels": None,
            "inactive_channels": None,
            "num_peers": None,
            "block_height": None,
            "channels": [],
        }

    headers = {"Grpc-Metadata-macaroon": MACAROON}

    async def lnd_get(path: str):
        try:
            async with httpx.AsyncClient(timeout=8.0, verify=False) as client:
                r = await client.get(f"{LND_REST}{path}", headers=headers)
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return None

    info, chans = await asyncio.gather(
        lnd_get("/v1/getinfo"),
        lnd_get("/v1/channels"),
        return_exceptions=True
    )

    if isinstance(info, Exception): info = None
    if isinstance(chans, Exception): chans = None

    channels = []
    if chans and "channels" in chans:
        for ch in chans["channels"]:
            channels.append({
                "active":         ch.get("active", False),
                "peer_alias":     ch.get("peer_alias", ""),
                "remote_pubkey":  ch.get("remote_pubkey", ""),
                "capacity":       int(ch.get("capacity", 0)),
                "local_balance":  int(ch.get("local_balance", 0)),
                "remote_balance": int(ch.get("remote_balance", 0)),
                "total_sent":     int(ch.get("total_satoshis_sent", 0)),
                "total_received": int(ch.get("total_satoshis_received", 0)),
            })

    return {
        "alias":             info.get("alias") if info else "boltwork",
        "synced":            info.get("synced_to_chain") if info else None,
        "synced_to_graph":   info.get("synced_to_graph") if info else None,
        "active_channels":   info.get("num_active_channels") if info else None,
        "inactive_channels": info.get("num_inactive_channels") if info else None,
        "num_peers":         info.get("num_peers") if info else None,
        "block_height":      info.get("block_height") if info else None,
        "version":           info.get("version", "").split(" ")[0] if info else None,
        "channels":          channels,
        "total_inbound":     sum(c["remote_balance"] for c in channels),
        "total_outbound":    sum(c["local_balance"] for c in channels),
        "total_capacity":    sum(c["capacity"] for c in channels),
    }
