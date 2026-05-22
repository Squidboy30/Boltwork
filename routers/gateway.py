"""
Boltwork Gateway Router
========================
Handles customer registration and dashboard for the L402 Gateway-as-a-Service.

Endpoints:
  POST /gateway/register          — Register API + endpoints, get gateway URL instantly
  GET  /gateway/dashboard/{id}    — Stats, endpoints, billing status
  GET  /gateway/config            — Generate current Aperture YAML (admin)
  POST /gateway/admin/activate    — Activate a pending customer (admin)
  GET  /gateway/health            — Gateway service health

Data stored in SQLite on the Fly volume at /data/gateway.db.
Config updates to parsebit-lnd are written via a shared volume or SSH —
see DEPLOY.md for the two-app config sync process.
"""

import json
import math
import os
import re
import smtplib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import httpx
import yaml
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, field_validator

router = APIRouter(prefix="/gateway", tags=["gateway"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GATEWAY_HOST = "parsebit-lnd.fly.dev"
SERVICE_URL = os.environ.get("SERVICE_URL", "https://parsebit.fly.dev")
ADMIN_TOKEN = os.environ.get("GATEWAY_ADMIN_TOKEN", "")
BOLTWORK_FEE = 0.02
BILLING_MINIMUM_SATS = 10_000
DB_PATH = Path(os.environ.get("GATEWAY_DB_PATH", "/data/gateway.db"))

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    api_base_url    TEXT NOT NULL,
    lightning_address TEXT,
    path_prefix     TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL,
    activated_at    TEXT,
    notes           TEXT
);
CREATE TABLE IF NOT EXISTS endpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    path            TEXT NOT NULL,
    method          TEXT NOT NULL DEFAULT 'POST',
    customer_price_sats  INTEGER NOT NULL,
    gross_price_sats     INTEGER NOT NULL,
    description     TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    UNIQUE(customer_id, path, method)
);
CREATE TABLE IF NOT EXISTS transactions (
    id              TEXT PRIMARY KEY,
    customer_id     TEXT NOT NULL,
    endpoint_path   TEXT NOT NULL,
    gross_sats      INTEGER NOT NULL,
    customer_sats   INTEGER NOT NULL,
    fee_sats        INTEGER NOT NULL,
    payment_hash    TEXT,
    ts              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txn_customer ON transactions(customer_id);
CREATE INDEX IF NOT EXISTS idx_txn_ts       ON transactions(ts);
CREATE INDEX IF NOT EXISTS idx_ep_customer  ON endpoints(customer_id);
"""


def _init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_fee(price: int) -> int:
    return max(math.ceil(price * (1 + BOLTWORK_FEE)), price + 1)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class EndpointDef(BaseModel):
    path: str
    method: str = "POST"
    price_sats: int
    description: str = ""

    @field_validator("path")
    @classmethod
    def valid_path(cls, v):
        v = v.strip()
        if not v.startswith("/"):
            v = "/" + v
        if not re.match(r'^/[a-zA-Z0-9/_\-\.]*$', v):
            raise ValueError(f"invalid path '{v}' — use only /a-z0-9_-./")
        return v

    @field_validator("method")
    @classmethod
    def valid_method(cls, v):
        v = v.upper()
        if v not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "*"}:
            raise ValueError(f"invalid method '{v}'")
        return v

    @field_validator("price_sats")
    @classmethod
    def valid_price(cls, v):
        if not isinstance(v, int) or v < 1 or v > 1_000_000:
            raise ValueError("price_sats must be 1–1,000,000")
        return v


class RegisterRequest(BaseModel):
    name: str
    email: str
    api_base_url: str
    lightning_address: Optional[str] = None
    endpoints: list[EndpointDef]

    @field_validator("name")
    @classmethod
    def valid_name(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("name must be at least 2 characters")
        return v

    @field_validator("api_base_url")
    @classmethod
    def valid_url(cls, v):
        v = v.strip().rstrip("/")
        if not v.startswith(("http://", "https://")):
            raise ValueError("api_base_url must start with http:// or https://")
        return v

    @field_validator("endpoints")
    @classmethod
    def has_endpoints(cls, v):
        if not v:
            raise ValueError("at least one endpoint is required")
        if len(v) > 50:
            raise ValueError("maximum 50 endpoints")
        return v


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_admin(x_admin_token: Optional[str] = Header(None)):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=500, detail="Admin token not configured")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def check_reachable(url: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                r = await client.head(url)
            except Exception:
                r = await client.get(url)
            if r.status_code < 500:
                return True, f"HTTP {r.status_code}"
            return False, f"HTTP {r.status_code}"
    except httpx.ConnectError:
        return False, "connection refused"
    except httpx.TimeoutException:
        return False, "timed out"
    except Exception as e:
        return False, str(e)[:80]


def send_confirmation(customer: dict, endpoints: list[dict]):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    if not smtp_user or not smtp_pass:
        print(f"[gateway] No SMTP configured — skipping confirmation for {customer['email']}")
        return

    gateway_base = f"https://{GATEWAY_HOST}{customer['path_prefix']}"
    first = endpoints[0]
    test_cmd = f"curl -X {first['method']} {gateway_base}{first['path']} -H 'Content-Type: application/json' -d '{{}}'"

    ep_lines = "\n".join(
        f"  {ep['method']} {gateway_base}{ep['path']} — {ep['customer_price_sats']} sats "
        f"(you earn) / {ep['gross_price_sats']} sats (charged)"
        for ep in endpoints
    )

    body = f"""Hi {customer['name']},

Your Boltwork L402 gateway is live.

Gateway base: {gateway_base}

Endpoints:
{ep_lines}

Test (should return HTTP 402 with a Lightning invoice):
{test_cmd}

Payment flow:
  1. Client hits your gateway URL → HTTP 402 + Lightning invoice
  2. Client pays invoice
  3. Client retries with: Authorization: L402 <token>:<preimage>
  4. Request proxied to your API, response returned

Dashboard: {SERVICE_URL}/gateway/dashboard/{customer['id']}

Pricing: you earn 98% of each transaction. Boltwork takes 2%, billed
monthly when accumulated fees exceed 10,000 sats.

— Boltwork by Cracked Minds
  crackedminds.co.uk
"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Your Boltwork L402 gateway is live"
    msg["From"] = smtp_user
    msg["To"] = customer["email"]
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(smtp_user, smtp_pass)
            s.sendmail(smtp_user, customer["email"], msg.as_string())
        print(f"[gateway] Confirmation sent to {customer['email']}")
    except Exception as e:
        print(f"[gateway] Email failed: {e}")


def build_aperture_services(customers: list[dict]) -> list[dict]:
    """Build full Aperture services list from customer records."""
    services = [
        {"name": "well-known",         "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/.well-known/.*$",      "address": "parsebit.fly.dev", "protocol": "https", "price": 0},
        {"name": "agent-spec",         "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/agent-spec\.md$",      "address": "parsebit.fly.dev", "protocol": "https", "price": 0},
        {"name": "llms-txt",           "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/llms\.txt$",           "address": "parsebit.fly.dev", "protocol": "https", "price": 0},
        {"name": "boltwork-sum-upload","hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/summarise/upload.*$",  "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(500)},
        {"name": "boltwork-sum-url",   "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/summarise/url.*$",     "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(500)},
        {"name": "boltwork-rev-code",  "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/review/code.*$",       "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(2000)},
        {"name": "boltwork-rev-url",   "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/review/url.*$",        "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(2000)},
        {"name": "boltwork-ext-web",   "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/extract/webpage.*$",   "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(100)},
        {"name": "boltwork-ext-data",  "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/extract/data.*$",      "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(200)},
        {"name": "boltwork-translate", "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/translate.*$",         "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(150)},
        {"name": "boltwork-an-tables", "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/analyse/tables.*$",    "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(300)},
        {"name": "boltwork-an-compare","hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/analyse/compare.*$",   "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(500)},
        {"name": "boltwork-an-explain","hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/analyse/explain.*$",   "address": "parsebit.fly.dev", "protocol": "https", "price": _apply_fee(500)},
    ]

    for c in customers:
        from urllib.parse import urlparse
        backend = urlparse(c["api_base_url"]).netloc
        prefix = c["path_prefix"]
        for ep in c["endpoints"]:
            full = f"{prefix}{ep['path']}"
            clean = re.sub(r'[^a-z0-9-]', '-', full.lower()).strip("-")[:48]
            name = f"{c['id'][5:13]}-{clean}"
            services.append({
                "name": name,
                "hostregexp": re.escape(GATEWAY_HOST),
                "pathregexp": f"^{re.escape(full)}.*$",
                "address": backend,
                "protocol": urlparse(c["api_base_url"]).scheme or "https",
                "price": ep["gross_price_sats"],
            })

    # Catch-all must be last
    services.append({"name": "public-catchall", "hostregexp": re.escape(GATEWAY_HOST), "pathregexp": r"^/.*$", "address": "parsebit.fly.dev", "protocol": "https", "price": 0})
    return services


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@router.on_event("startup")
async def startup():
    _init_db()
    print("[gateway] Database initialised")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register")
async def register(body: RegisterRequest, background_tasks: BackgroundTasks):
    """
    Register for a Boltwork L402 gateway.

    Validates config, provisions gateway URL, stores customer record,
    and sends confirmation email with test instructions.
    """
    _init_db()

    # Duplicate check
    with _db() as conn:
        existing = conn.execute("SELECT id FROM customers WHERE email=?",
                                (body.email.lower().strip(),)).fetchone()
    if existing:
        raise HTTPException(status_code=409,
            detail=f"Account already exists for {body.email}. Email support@crackedminds.co.uk to update.")

    # Check backend is reachable
    reachable, detail = await check_reachable(body.api_base_url)
    if not reachable:
        raise HTTPException(status_code=422,
            detail=f"Cannot reach {body.api_base_url}: {detail}. Ensure it's publicly accessible.")

    # Create customer
    customer_id = "cust_" + uuid.uuid4().hex[:12]
    path_prefix = "/c/" + uuid.uuid4().hex[:8]

    with _db() as conn:
        conn.execute(
            "INSERT INTO customers (id,email,name,api_base_url,lightning_address,path_prefix,status,created_at,activated_at) VALUES (?,?,?,?,?,?,'active',?,?)",
            (customer_id, body.email.lower().strip(), body.name.strip(),
             body.api_base_url, body.lightning_address, path_prefix, _now(), _now())
        )
        added = []
        for ep in body.endpoints:
            gross = _apply_fee(ep.price_sats)
            conn.execute(
                "INSERT INTO endpoints (customer_id,path,method,customer_price_sats,gross_price_sats,description,active,created_at) VALUES (?,?,?,?,?,?,1,?)",
                (customer_id, ep.path, ep.method, ep.price_sats, gross, ep.description, _now())
            )
            added.append({"path": ep.path, "method": ep.method,
                          "customer_price_sats": ep.price_sats, "gross_price_sats": gross,
                          "description": ep.description})

    customer = {"id": customer_id, "name": body.name, "email": body.email.lower().strip(),
                "api_base_url": body.api_base_url, "path_prefix": path_prefix}

    background_tasks.add_task(send_confirmation, customer, added)

    gateway_base = f"https://{GATEWAY_HOST}{path_prefix}"
    first = added[0]

    return {
        "status": "active",
        "customer_id": customer_id,
        "path_prefix": path_prefix,
        "gateway_base_url": gateway_base,
        "endpoints": [
            {
                "path": ep["path"],
                "method": ep["method"],
                "gateway_url": f"{gateway_base}{ep['path']}",
                "customer_earns_sats": ep["customer_price_sats"],
                "total_charged_sats": ep["gross_price_sats"],
            }
            for ep in added
        ],
        "test_command": f"curl -X {first['method']} {gateway_base}{first['path']} -H 'Content-Type: application/json' -d '{{}}'",
        "dashboard_url": f"{SERVICE_URL}/gateway/dashboard/{customer_id}",
        "next_step": "Your gateway is live. The test command above should return HTTP 402 within 30 seconds.",
        
    }


@router.get("/dashboard/{customer_id}")
async def dashboard(customer_id: str):
    """Customer dashboard — stats, endpoints, health, billing."""
    _init_db()
    with _db() as conn:
        customer = conn.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")
        customer = dict(customer)

        endpoints = [dict(r) for r in conn.execute(
            "SELECT * FROM endpoints WHERE customer_id=? AND active=1", (customer_id,)
        ).fetchall()]

        since_30d = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        since_7d  = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

        stats_30d = dict(conn.execute(
            "SELECT COUNT(*) as requests, COALESCE(SUM(gross_sats),0) as gross, COALESCE(SUM(fee_sats),0) as fees FROM transactions WHERE customer_id=? AND ts>=?",
            (customer_id, since_30d)
        ).fetchone())

        stats_7d = dict(conn.execute(
            "SELECT COUNT(*) as requests, COALESCE(SUM(gross_sats),0) as gross FROM transactions WHERE customer_id=? AND ts>=?",
            (customer_id, since_7d)
        ).fetchone())

        total_fees = dict(conn.execute(
            "SELECT COALESCE(SUM(fee_sats),0) as total FROM transactions WHERE customer_id=?",
            (customer_id,)
        ).fetchone())["total"]

    gateway_base = f"https://{GATEWAY_HOST}{customer['path_prefix']}"

    return {
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "status": customer["status"],
            "gateway_base_url": gateway_base,
            "activated_at": customer["activated_at"],
        },
        "endpoints": [
            {
                "path": ep["path"],
                "method": ep["method"],
                "gateway_url": f"{gateway_base}{ep['path']}",
                "customer_earns_sats": ep["customer_price_sats"],
                "total_charged_sats": ep["gross_price_sats"],
            }
            for ep in endpoints
        ],
        "stats_30d": {
            "total_requests": stats_30d["requests"],
            "total_gross_sats": stats_30d["gross"],
            "total_fee_sats": stats_30d["fees"],
        },
        "stats_7d": {
            "total_requests": stats_7d["requests"],
            "total_gross_sats": stats_7d["gross"],
        },
        "billing": {
            "total_unbilled_fee_sats": total_fees,
            "billing_minimum_sats": BILLING_MINIMUM_SATS,
            "above_minimum": total_fees >= BILLING_MINIMUM_SATS,
            "note": "Billed monthly when accumulated fees exceed 10,000 sats.",
        },
    }


@router.get("/config", response_class=PlainTextResponse, dependencies=[Depends(require_admin)])
async def get_config():
    """Generate current Aperture YAML from all active customers. Admin only."""
    _init_db()
    with _db() as conn:
        customers_raw = [dict(r) for r in conn.execute(
            "SELECT * FROM customers WHERE status='active'"
        ).fetchall()]

    customers = []
    for c in customers_raw:
        eps = []
        with _db() as conn:
            for ep in conn.execute(
                "SELECT * FROM endpoints WHERE customer_id=? AND active=1", (c["id"],)
            ).fetchall():
                eps.append(dict(ep))
        c["endpoints"] = eps
        customers.append(c)

    services = build_aperture_services(customers)
    config = {
        "listenaddr": "127.0.0.1:8080",
        "debuglevel": "info",
        "autocert": False,
        "insecure": True,
        "authenticator": {
            "network": "mainnet",
            "lndhost": "localhost:10009",
            "tlspath": "/root/.lnd/tls.cert",
            "macdir": "/root/.lnd/data/chain/bitcoin/mainnet/",
            "disable": False,
        },
        "dbbackend": "sqlite",
        "sqlite": {"dbfile": "/root/.lnd/.aperture/aperture.db"},
        "services": services,
    }
    return yaml.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False)


@router.get("/admin/customers", dependencies=[Depends(require_admin)])
async def admin_customers():
    _init_db()
    with _db() as conn:
        customers = [dict(r) for r in conn.execute(
            "SELECT id,name,email,status,path_prefix,created_at,activated_at FROM customers ORDER BY created_at DESC"
        ).fetchall()]
    return {"customers": customers, "total": len(customers)}


@router.get("/health")
async def gateway_health():
    aperture_ok = False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"https://{GATEWAY_HOST}/health",
                headers={"Content-Type": "application/json"},
            )
            aperture_ok = r.status_code < 500
    except Exception:
        pass
    return {
        "status": "ok" if aperture_ok else "degraded",
        "gateway_host": GATEWAY_HOST,
        "aperture": "ok" if aperture_ok else "unreachable",
    }
