"""
routers/credits.py — Stripe credit purchase + API key management
"""
import os, json, time, secrets, hashlib, sqlite3
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import httpx

router = APIRouter(tags=["credits"])

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DB_PATH = os.environ.get("CREDITS_DB_PATH", "/data/credits.db")

PRICE_CREDITS = {
    "price_1TjJalH5v95zsFpRtnAjSSOv": 10000,
    "price_1TjJalH5v95zsFpR6QObi6d1": 50000,
    "price_1TjJalH5v95zsFpR8DTGvxjc": 200000,
}

ENDPOINT_COSTS = {
    "/summarise/upload": 500,
    "/summarise/url": 500,
    "/review/code": 2000,
    "/review/url": 2000,
    "/extract/webpage": 100,
    "/extract/data": 200,
    "/translate": 150,
    "/analyse/tables": 300,
    "/analyse/compare": 500,
    "/analyse/explain": 500,
    "/analyse/image": 200,
    "/memory/store": 10,
    "/memory/retrieve": 5,
    "/memory/delete": 0,
    "/workflow/run": 1000,
}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_keys (
            key_hash    TEXT PRIMARY KEY,
            key_prefix  TEXT NOT NULL,
            email       TEXT NOT NULL,
            credits     INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL,
            last_used   INTEGER,
            total_calls INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            session_id  TEXT PRIMARY KEY,
            price_id    TEXT NOT NULL,
            email       TEXT,
            redeemed    INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def generate_api_key():
    raw = secrets.token_urlsafe(32)
    key = f"bw_{raw}"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    prefix = key[:12]
    return key, key_hash, prefix


def verify_api_key(api_key: str) -> Optional[dict]:
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    db = get_db()
    row = db.execute(
        "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    db.close()
    if not row:
        return None
    return dict(row)


def deduct_credits(key_hash: str, endpoint: str) -> bool:
    cost = ENDPOINT_COSTS.get(endpoint, 0)
    if cost == 0:
        return True
    db = get_db()
    row = db.execute(
        "SELECT credits FROM api_keys WHERE key_hash = ?", (key_hash,)
    ).fetchone()
    if not row or row["credits"] < cost:
        db.close()
        return False
    db.execute(
        "UPDATE api_keys SET credits = credits - ?, last_used = ?, total_calls = total_calls + 1 WHERE key_hash = ?",
        (cost, int(time.time()), key_hash)
    )
    db.commit()
    db.close()
    return True


# ── Routes ────────────────────────────────────────────────────

class RedeemRequest(BaseModel):
    session_id: str
    email: str

@router.post("/credits/redeem")
async def redeem_credits(body: RedeemRequest):
    """Called after successful Stripe payment to issue an API key."""
    db = get_db()
    session = db.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (body.session_id,)
    ).fetchone()

    if not session:
        # Verify with Stripe directly
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"https://api.stripe.com/v1/checkout/sessions/{body.session_id}",
                headers={"Authorization": f"Bearer {STRIPE_SECRET}"}
            )
        if not r.is_success:
            raise HTTPException(400, "Invalid session")
        stripe_session = r.json()
        if stripe_session.get("payment_status") != "paid":
            raise HTTPException(400, "Payment not confirmed")
        price_id = stripe_session["line_items"]["data"][0]["price"]["id"] if "line_items" in stripe_session else None
        if not price_id:
            # Get line items separately
            async with httpx.AsyncClient() as client:
                r2 = await client.get(
                    f"https://api.stripe.com/v1/checkout/sessions/{body.session_id}/line_items",
                    headers={"Authorization": f"Bearer {STRIPE_SECRET}"}
                )
            items = r2.json()
            price_id = items["data"][0]["price"]["id"]
        credits = PRICE_CREDITS.get(price_id, 0)
        if credits == 0:
            raise HTTPException(400, "Unknown price")
        db.execute(
            "INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, 0, ?)",
            (body.session_id, price_id, body.email, int(time.time()))
        )
        db.commit()
        session = db.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (body.session_id,)
        ).fetchone()

    if session["redeemed"]:
        raise HTTPException(400, "Session already redeemed")

    credits = PRICE_CREDITS.get(session["price_id"], 0)
    api_key, key_hash, prefix = generate_api_key()

    db.execute(
        "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, NULL, 0)",
        (key_hash, prefix, body.email, credits, int(time.time()))
    )
    db.execute(
        "UPDATE sessions SET redeemed = 1, email = ? WHERE session_id = ?",
        (body.email, body.session_id)
    )
    db.commit()
    db.close()

    return {
        "api_key": api_key,
        "credits": credits,
        "email": body.email,
        "message": f"API key issued with {credits:,} sats credits. Add to your MCP config as BOLTWORK_API_KEY."
    }


@router.get("/credits/balance")
async def check_balance(x_api_key: str = Header(...)):
    """Check remaining credits for an API key."""
    record = verify_api_key(x_api_key)
    if not record:
        raise HTTPException(401, "Invalid API key")
    return {
        "credits": record["credits"],
        "total_calls": record["total_calls"],
        "key_prefix": record["key_prefix"],
        "email": record["email"],
    }


@router.get("/credits/costs")
async def get_costs():
    """Return the sats cost for each endpoint."""
    return {"costs": ENDPOINT_COSTS, "currency": "sats"}
