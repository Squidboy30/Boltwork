"""
Boltwork Pay Router
====================
Hosted payment page for L402 invoices.

GET  /pay              - Landing page explaining L402 and linking to demo
GET  /pay/<r_hash>     - Payment page for a specific invoice (not yet implemented — show instructions)

This router exists to reduce friction for developers who hit a 402 and want
to understand what happened and how to pay.
"""

import os
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["pay"])

SERVICE_URL = os.environ.get("SERVICE_URL", "https://parsebit.fly.dev")
DEMO_URL    = "https://crackedminds.co.uk/gateway/demo.html"


@router.get("/pay", response_class=HTMLResponse)
def pay_landing():
    """
    Human-readable landing page explaining L402 and how to pay.
    Linked from the WWW-Authenticate header hint.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Boltwork — Payment Required</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0f0f0f;color:#f0f0ec;padding:2rem 1rem;min-height:100vh}}
.container{{max-width:580px;margin:0 auto}}
.badge{{display:inline-block;background:#f59e0b;color:#000;font-size:11px;font-weight:700;padding:4px 10px;border-radius:20px;letter-spacing:.06em;text-transform:uppercase;margin-bottom:1rem}}
h1{{font-size:24px;font-weight:600;margin-bottom:8px}}
.sub{{font-size:15px;color:#888;margin-bottom:2rem;line-height:1.5}}
.card{{background:#1a1a1a;border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:20px;margin-bottom:16px}}
.card-title{{font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#555;margin-bottom:12px}}
.step{{display:flex;gap:12px;align-items:flex-start;margin-bottom:12px}}
.step:last-child{{margin-bottom:0}}
.step-n{{flex-shrink:0;width:24px;height:24px;border-radius:50%;background:#f59e0b;color:#000;font-size:12px;font-weight:700;display:flex;align-items:center;justify-content:center}}
.step-text{{font-size:13px;color:#ccc;line-height:1.5}}
.step-text code{{background:#222;padding:2px 6px;border-radius:4px;font-family:monospace;font-size:12px;color:#9fe0c8}}
.btn{{display:inline-flex;align-items:center;gap:6px;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:500;text-decoration:none;margin-right:8px;margin-top:4px}}
.btn-gold{{background:#f59e0b;color:#000}}
.btn-ghost{{background:#222;border:1px solid rgba(255,255,255,0.1);color:#ccc}}
.code-block{{background:#111;border:1px solid rgba(255,255,255,0.06);border-radius:8px;padding:14px;font-family:monospace;font-size:12px;color:#9fe0c8;overflow-x:auto;margin-top:8px;line-height:1.6}}
footer{{margin-top:2rem;padding-top:1rem;border-top:1px solid rgba(255,255,255,0.06);font-size:12px;color:#444;text-align:center}}
footer a{{color:#666;text-decoration:none}}
</style>
</head>
<body>
<div class="container">
  <div class="badge">⚡ 402 Payment Required</div>
  <h1>This API requires a Lightning payment</h1>
  <p class="sub">Boltwork uses the L402 protocol — pay a small Bitcoin Lightning invoice to get your result. No account needed.</p>

  <div class="card">
    <div class="card-title">How it works</div>
    <div class="step"><div class="step-n">1</div><div class="step-text">Make your API request — you receive a <code>402</code> response with a Lightning invoice in the <code>WWW-Authenticate</code> header.</div></div>
    <div class="step"><div class="step-n">2</div><div class="step-text">Pay the invoice using any Lightning wallet (Alby, Strike, Phoenix, LNbits, Mutiny). You receive a preimage as proof of payment.</div></div>
    <div class="step"><div class="step-n">3</div><div class="step-text">Retry your request with <code>Authorization: L402 &lt;macaroon&gt;:&lt;preimage&gt;</code> — you get your JSON result.</div></div>
  </div>

  <div class="card">
    <div class="card-title">Try it now — interactive demo</div>
    <p style="font-size:13px;color:#888;margin-bottom:12px">See the full payment flow in your browser. Supports Alby WebLN for 1-click payment, or any wallet manually.</p>
    <a href="{DEMO_URL}" class="btn btn-gold">⚡ Open live demo</a>
  </div>

  <div class="card">
    <div class="card-title">Free trial — no wallet needed</div>
    <p style="font-size:13px;color:#888;margin-bottom:12px">Try two endpoints free (rate limited to 5 calls/hour):</p>
    <div class="code-block">POST {SERVICE_URL}/trial/summarise
{{"text": "your text here"}}

POST {SERVICE_URL}/trial/review
{{"code": "your code here"}}</div>
  </div>

  <div class="card">
    <div class="card-title">Automate payments — boltwork-mcp</div>
    <p style="font-size:13px;color:#888;margin-bottom:8px">Use boltwork as an MCP tool in Claude, Cursor, or Windsurf. Payments happen automatically via your configured wallet.</p>
    <div class="code-block">pip install boltwork-mcp
# Set NWC_CONNECTION_STRING, LNBITS_API_KEY, or STRIKE_API_KEY</div>
    <a href="https://github.com/Squidboy30/boltwork-mcp" class="btn btn-ghost" style="margin-top:10px">📦 boltwork-mcp on GitHub</a>
    <a href="{SERVICE_URL}/agent-spec.md" class="btn btn-ghost" style="margin-top:10px">📄 API docs</a>
  </div>

  <div class="card">
    <div class="card-title">curl example</div>
    <div class="code-block"># Step 1: get invoice
curl -X POST {SERVICE_URL}/extract/webpage \
  -H "Content-Type: application/json" \
  -d '{{"url":"https://example.com"}}'
# → 402 + WWW-Authenticate: L402 macaroon="...", invoice="lnbc..."

# Step 2: pay invoice with your wallet, get preimage

# Step 3: retry with credentials
curl -X POST {SERVICE_URL}/extract/webpage \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 &lt;macaroon&gt;:&lt;preimage&gt;" \
  -d '{{"url":"https://example.com"}}'
# → 200 + JSON result</div>
  </div>

  <footer>
    <a href="{SERVICE_URL}">Boltwork API</a> &nbsp;·&nbsp;
    <a href="{SERVICE_URL}/.well-known/l402.json">L402 manifest</a> &nbsp;·&nbsp;
    <a href="https://crackedminds.co.uk">Cracked Minds</a>
  </footer>
</div>
</body>
</html>"""
