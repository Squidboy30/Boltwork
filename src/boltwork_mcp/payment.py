"""
Boltwork MCP - L402 Payment Handler
=====================================
Handles the full L402 payment flow transparently.

Supported wallet backends:
  - NWC  (Nostr Wallet Connect) - works with Alby, Mutiny, etc.
  - Phoenixd - self-hosted Lightning node

Flow:
  1. Request hits L402 gateway → 402 response
  2. Extract macaroon + invoice from WWW-Authenticate header
  3. Pay invoice via configured wallet backend
  4. Return Authorization header value for retry
"""

import os
import re
import json
import asyncio
import httpx
from typing import Optional


# ---------------------------------------------------------------------------
# WWW-Authenticate header parsing
# ---------------------------------------------------------------------------

def parse_402(www_authenticate: str) -> tuple[str, str]:
    """
    Parse the WWW-Authenticate header from a 402 response.
    Returns (macaroon, invoice).
    
    Header format:
      L402 macaroon="<base64>", invoice="<bolt11>"
    """
    macaroon_match = re.search(r'macaroon="([^"]+)"', www_authenticate)
    invoice_match  = re.search(r'invoice="([^"]+)"', www_authenticate)

    if not macaroon_match or not invoice_match:
        raise ValueError(
            f"Could not parse L402 header: {www_authenticate[:200]}"
        )

    return macaroon_match.group(1), invoice_match.group(1)


# ---------------------------------------------------------------------------
# NWC payment backend
# ---------------------------------------------------------------------------

async def pay_invoice_nwc(invoice: str, nwc_string: str) -> str:
    """
    Pay a Lightning invoice via Nostr Wallet Connect.
    Returns the payment preimage as a hex string.

    NWC connection string format:
      nostr+walletconnect://<pubkey>?relay=<relay_url>&secret=<secret>

    Uses the pynostr-based NWC flow via a lightweight websocket exchange.
    """
    try:
        from pynostr.key import PrivateKey
        from pynostr.encrypted_dm import EncryptedDirectMessage
        import websockets
        import uuid
        import time
    except ImportError:
        raise ImportError(
            "NWC requires: pip install pynostr websockets\n"
            "Or install the full package: pip install boltwork-mcp[nwc]"
        )

    # Parse NWC connection string
    # nostr+walletconnect://<wallet_pubkey>?relay=<url>&secret=<hex_secret>
    match = re.match(
        r"nostr\+walletconnect://([0-9a-fA-F]+)\?.*relay=([^&]+).*secret=([0-9a-fA-F]+)",
        nwc_string
    )
    if not match:
        raise ValueError("Invalid NWC connection string format")

    wallet_pubkey_hex = match.group(1)
    relay_url         = match.group(2).rstrip("/")
    secret_hex        = match.group(3)

    client_privkey = PrivateKey(bytes.fromhex(secret_hex))
    client_pubkey  = client_privkey.public_key.hex()

    # Build pay_invoice request
    request_id = str(uuid.uuid4())
    payload    = json.dumps({
        "id":     request_id,
        "method": "pay_invoice",
        "params": {"invoice": invoice},
    })

    # Encrypt the request to the wallet pubkey
    dm = EncryptedDirectMessage(
        recipient_pubkey=wallet_pubkey_hex,
        cleartext_content=payload,
    )
    dm.encrypt(client_privkey.hex())

    event = dm.to_event()
    event.sign(client_privkey.hex())

    timeout    = 30.0
    deadline   = asyncio.get_event_loop().time() + timeout
    preimage   = None

    async with websockets.connect(relay_url) as ws:
        # Subscribe to responses addressed to us from the wallet
        sub_id  = str(uuid.uuid4())[:8]
        sub_msg = json.dumps([
            "REQ", sub_id,
            {"kinds": [23195], "#p": [client_pubkey], "since": int(time.time()) - 5}
        ])
        await ws.send(sub_msg)

        # Send the payment request
        await ws.send(json.dumps(["EVENT", event.to_dict()]))

        # Wait for response
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg[0] == "EVENT" and msg[1] == sub_id:
                    ev = msg[2]
                    # Decrypt response
                    dm_resp = EncryptedDirectMessage.from_event_dict(ev)
                    dm_resp.decrypt(client_privkey.hex(), public_key_hex=wallet_pubkey_hex)
                    resp = json.loads(dm_resp.cleartext_content)
                    if resp.get("result_type") == "pay_invoice":
                        if "error" in resp:
                            raise RuntimeError(f"NWC payment failed: {resp['error']}")
                        preimage = resp["result"]["preimage"]
                        break
            except asyncio.TimeoutError:
                continue

    if not preimage:
        raise TimeoutError("NWC payment timed out after 30s")

    return preimage


# ---------------------------------------------------------------------------
# Phoenixd payment backend
# ---------------------------------------------------------------------------

async def pay_invoice_phoenixd(invoice: str, phoenixd_url: str, phoenixd_password: str) -> str:
    """
    Pay a Lightning invoice via Phoenixd REST API.
    Returns the payment preimage as a hex string.

    phoenixd_url:      e.g. http://localhost:9740
    phoenixd_password: the HTTP Basic auth password from phoenixd config
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{phoenixd_url}/payinvoice",
            data={"invoice": invoice},
            auth=("", phoenixd_password),
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Phoenixd payment failed: HTTP {response.status_code} — {response.text[:200]}"
            )
        data = response.json()
        if "preimage" not in data:
            raise RuntimeError(f"Phoenixd response missing preimage: {data}")
        return data["preimage"]


# ---------------------------------------------------------------------------
# Main payment dispatcher
# ---------------------------------------------------------------------------

async def pay_invoice(invoice: str) -> str:
    """
    Pay a Lightning invoice using the configured wallet backend.
    Returns the preimage as a hex string.

    Reads configuration from environment variables:
      NWC_CONNECTION_STRING  — use NWC backend
      PHOENIXD_URL           — use Phoenixd backend (also needs PHOENIXD_PASSWORD)
      PHOENIXD_PASSWORD      — Phoenixd HTTP Basic auth password
    """
    nwc_string        = os.environ.get("NWC_CONNECTION_STRING", "").strip()
    phoenixd_url      = os.environ.get("PHOENIXD_URL", "").strip()
    phoenixd_password = os.environ.get("PHOENIXD_PASSWORD", "").strip()

    if nwc_string:
        return await pay_invoice_nwc(invoice, nwc_string)
    elif phoenixd_url and phoenixd_password:
        return await pay_invoice_phoenixd(invoice, phoenixd_url, phoenixd_password)
    else:
        raise RuntimeError(
            "No wallet configured. Set one of:\n"
            "  NWC_CONNECTION_STRING=nostr+walletconnect://...\n"
            "  PHOENIXD_URL=http://localhost:9740 + PHOENIXD_PASSWORD=..."
        )


# ---------------------------------------------------------------------------
# Full L402 request helper — use this from tool handlers
# ---------------------------------------------------------------------------

async def l402_request(
    method: str,
    url: str,
    gateway_url: str,
    json_body: Optional[dict] = None,
    files: Optional[dict] = None,
) -> dict:
    """
    Make an L402-authenticated request.

    1. Sends request to gateway_url (the L402 gateway)
    2. If 402, pays the invoice and retries with credentials
    3. Returns the parsed JSON response

    Args:
        method:      HTTP method ("GET", "POST")
        url:         The logical endpoint path (e.g. "/summarise/url")
        gateway_url: Full URL to the L402 gateway endpoint
        json_body:   JSON request body (for POST)
        files:       Multipart files (for file upload)
    """
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:

        # First attempt — expect either 200 or 402
        kwargs = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if files is not None:
            kwargs["files"] = files

        response = await client.request(method, gateway_url, **kwargs)

        if response.status_code == 200:
            return response.json()

        if response.status_code != 402:
            raise RuntimeError(
                f"Unexpected HTTP {response.status_code} from {gateway_url}: "
                f"{response.text[:300]}"
            )

        # Parse 402 and pay
        www_auth = response.headers.get("WWW-Authenticate", "")
        if not www_auth:
            raise RuntimeError("Got 402 but no WWW-Authenticate header")

        macaroon, invoice = parse_402(www_auth)
        preimage = await pay_invoice(invoice)

        # Retry with L402 credentials
        auth_header = f"L402 {macaroon}:{preimage}"
        response2 = await client.request(
            method, gateway_url,
            headers={"Authorization": auth_header},
            **kwargs,
        )

        if response2.status_code != 200:
            raise RuntimeError(
                f"L402 retry failed: HTTP {response2.status_code} — "
                f"{response2.text[:300]}"
            )

        return response2.json()
