"""
Boltwork Demo Router
=====================
Server-side payment proxy for the demo page.

Endpoints:
  POST /demo/pay    - Pay a Lightning invoice using the pre-loaded demo wallet
  GET  /demo/status - Check demo wallet balance and rate limit status

Rate limiting: 1 free payment per IP per 24h, stored in SQLite.
Balance check: if demo wallet < 1000 sats, demo is disabled.
"""

import os
import time
import sqlite3
import httpx
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/demo", tags=["demo"])

LNBITS_URL = os.environ.get("DEMO_LNBITS_URL", "https://lnbits.com").rstrip("/")
LNBITS_KEY = os.environ.get("DEMO_LNBITS_KEY", "")
DB_PATH    = Path(os.environ.get("MEMORY_DB_PATH", "/data/gateway.db"))
MIN_BALANCE_SATS = 1000
RATE_LIMIT_HOURS = 24


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _ensure_demo_table():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS demo_payments (
                ip          TEXT NOT NULL,
                paid_at     TEXT NOT NULL,
                invoice     TEXT,
                amount_sats INTEGER DEFAULT 0
            )
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass

_ensure_demo_table()


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _ip_used_demo(ip: str) -> bool:
    """Return True if this IP has used the demo in the last 24h."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=RATE_LIMIT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COUNT(*) FROM demo_payments WHERE ip=? AND paid_at > ?",
            (ip, cutoff)
        ).fetchone()
        conn.close()
        return (row[0] or 0) > 0
    except Exception:
        return False


def _record_demo_payment(ip: str, invoice: str, amount_sats: int):
    try:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO demo_payments (ip, paid_at, invoice, amount_sats) VALUES (?, ?, ?, ?)",
            (ip, now, invoice, amount_sats)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LNbits helpers
# ---------------------------------------------------------------------------

async def _get_balance() -> int:
    """Get LNbits wallet balance in sats."""
    if not LNBITS_KEY:
        return 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{LNBITS_URL}/api/v1/wallet",
                headers={"X-Api-Key": LNBITS_KEY}
            )
            if r.status_code == 200:
                return r.json().get("balance", 0) // 1000  # msat to sat
    except Exception:
        pass
    return 0


async def _pay_invoice(invoice: str) -> str:
    """Pay a bolt11 invoice from LNbits wallet. Returns preimage."""
    import asyncio
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"{LNBITS_URL}/api/v1/payments",
            json={"out": True, "bolt11": invoice},
            headers={"X-Api-Key": LNBITS_KEY, "Content-Type": "application/json"}
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=402, detail=f"Demo payment failed: {r.text[:200]}")
        payment_hash = r.json().get("payment_hash")
        if not payment_hash:
            raise HTTPException(status_code=500, detail="No payment_hash from LNbits")

        for _ in range(15):
            await asyncio.sleep(1.0)
            r2 = await client.get(
                f"{LNBITS_URL}/api/v1/payments/{payment_hash}",
                headers={"X-Api-Key": LNBITS_KEY}
            )
            if r2.status_code == 200:
                pdata = r2.json()
                if pdata.get("paid"):
                    preimage = pdata.get("details", {}).get("preimage") or pdata.get("preimage")
                    if preimage:
                        return preimage
        raise HTTPException(status_code=500, detail="Payment sent but preimage not found")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class PayRequest(BaseModel):
    invoice: str
    amount_sats: int = 0


@router.get("/status")
async def demo_status(request: Request):
    """Check demo wallet availability."""
    if not LNBITS_KEY:
        return JSONResponse({"available": False, "reason": "Demo wallet not configured"})

    balance = await _get_balance()
    ip = _get_client_ip(request)
    used = _ip_used_demo(ip)

    return JSONResponse({
        "available": balance >= MIN_BALANCE_SATS and not used,
        "balance_sats": balance,
        "low_balance": balance < MIN_BALANCE_SATS,
        "rate_limited": used,
        "rate_limit_hours": RATE_LIMIT_HOURS,
    })


@router.post("/pay")
async def demo_pay(body: PayRequest, request: Request):
    """
    Pay a Lightning invoice using the pre-loaded demo wallet.
    Rate limited to 1 payment per IP per 24h.
    """
    if not LNBITS_KEY:
        raise HTTPException(status_code=503, detail="Demo wallet not configured")

    ip = _get_client_ip(request)

    # Rate limit check
    if _ip_used_demo(ip):
        raise HTTPException(
            status_code=429,
            detail="Demo limit reached — 1 free payment per 24h. Use your own wallet to continue."
        )

    # Balance check
    balance = await _get_balance()
    if balance < MIN_BALANCE_SATS:
        raise HTTPException(
            status_code=503,
            detail="Demo wallet is low on funds. Use your own wallet — see parsebit.fly.dev/pay"
        )

    if not body.invoice.startswith("lnbc"):
        raise HTTPException(status_code=400, detail="Invalid Lightning invoice")

    preimage = await _pay_invoice(body.invoice)
    _record_demo_payment(ip, body.invoice, body.amount_sats)

    return JSONResponse({
        "preimage": preimage,
        "paid": True,
        "message": "Payment successful — your result is on its way"
    })
