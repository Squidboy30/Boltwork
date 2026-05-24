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

STRIKE_API_KEY = os.environ.get("DEMO_STRIKE_KEY", "")
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
    """Get Strike account balance in sats."""
    if not STRIKE_API_KEY:
        return 0
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.strike.me/v1/balances",
                headers={"Authorization": f"Bearer {STRIKE_API_KEY}"}
            )
            if r.status_code == 200:
                balances = r.json()
                for b in balances:
                    if b.get("currency") == "BTC":
                        btc = float(b.get("available", 0))
                        return int(btc * 100_000_000)  # BTC to sats
    except Exception:
        pass
    return 0


async def _pay_invoice(invoice: str) -> str:
    """Pay a bolt11 invoice via Strike API. Returns preimage."""
    import asyncio
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Create payment quote
        quote_resp = await client.post(
            "https://api.strike.me/v1/payment-quotes/lightning",
            json={"lnInvoice": invoice, "sourceCurrency": "BTC"},
            headers={"Authorization": f"Bearer {STRIKE_API_KEY}", "Content-Type": "application/json"},
        )
        if quote_resp.status_code not in (200, 201):
            raise HTTPException(status_code=402, detail=f"Demo payment failed: {quote_resp.text[:200]}")
        quote = quote_resp.json()
        quote_id = quote.get("paymentQuoteId")
        if not quote_id:
            raise HTTPException(status_code=500, detail="No paymentQuoteId from Strike")

        # Execute payment
        pay_resp = await client.patch(
            f"https://api.strike.me/v1/payment-quotes/{quote_id}/execute",
            headers={"Authorization": f"Bearer {STRIKE_API_KEY}"},
        )
        if pay_resp.status_code not in (200, 201):
            raise HTTPException(status_code=402, detail=f"Strike execution failed: {pay_resp.text[:200]}")
        pay_data = pay_resp.json()
        payment_id = pay_data.get("paymentId")
        if not payment_id:
            raise HTTPException(status_code=500, detail="No paymentId from Strike")

        # Poll for preimage
        for _ in range(15):
            await asyncio.sleep(1.0)
            status_resp = await client.get(
                f"https://api.strike.me/v1/payments/{payment_id}",
                headers={"Authorization": f"Bearer {STRIKE_API_KEY}"},
            )
            if status_resp.status_code == 200:
                s = status_resp.json()
                if s.get("state") == "COMPLETED":
                    preimage = s.get("lightning", {}).get("preimage")
                    if preimage:
                        return preimage
                elif s.get("state") in ("FAILED", "CANCELLED"):
                    raise HTTPException(status_code=402, detail=f"Strike payment {s.get('state')}")
        raise HTTPException(status_code=500, detail="Strike payment did not complete in time")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class PayRequest(BaseModel):
    invoice: str
    amount_sats: int = 0


@router.get("/status")
async def demo_status(request: Request):
    """Check demo wallet availability."""
    if not STRIKE_API_KEY:
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
    if not STRIKE_API_KEY:
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
