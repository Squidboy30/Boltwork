"""
Boltwork Daily Usage Report
=============================
Sends a daily email with PyPI download stats, Fly.io API usage,
sats earned, and per-endpoint breakdown.

Runs via GitHub Actions at 8am UK time (same schedule as check_health.py).

Requires the same secrets already set for check_health.py:
    GMAIL_USER          — sender Gmail address
    GMAIL_APP_PASSWORD  — Gmail app password
    FLY_API_TOKEN       — Fly.io personal token (fly auth token)

Optional:
    NOTIFY_EMAIL        — recipient (defaults to GMAIL_USER)
    LOOKBACK_HOURS      — hours of logs to scan (default 24)
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# ---------------------------------------------------------------------------
# Config — same env vars as check_health.py
# ---------------------------------------------------------------------------

GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)
FLY_API_TOKEN  = os.environ.get("FLY_API_TOKEN", "")

FLY_APP_NAME   = "parsebit"
BOLTWORK_API   = "https://parsebit.fly.dev"
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
TIMEOUT        = 15

PRICE_MAP = {
    "/summarise/upload":  500,
    "/summarise/url":     500,
    "/review/code":      2000,
    "/review/url":       2000,
    "/extract/webpage":   100,
    "/extract/data":      200,
    "/translate":         150,
    "/analyse/tables":    300,
    "/analyse/compare":   500,
    "/analyse/explain":   500,
}

# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8")

# ---------------------------------------------------------------------------
# PyPI stats
# ---------------------------------------------------------------------------

def get_pypi_stats():
    result = {"ok": False}
    try:
        _, body = fetch("https://pypistats.org/api/packages/boltwork-mcp/recent")
        data = json.loads(body).get("data", {})
        result.update({
            "ok":         True,
            "last_day":   data.get("last_day", 0),
            "last_week":  data.get("last_week", 0),
            "last_month": data.get("last_month", 0),
        })
    except Exception as e:
        result["error"] = str(e)

    try:
        _, body = fetch("https://pypistats.org/api/packages/boltwork-mcp/overall")
        rows = json.loads(body).get("data", [])
        result["all_time"] = sum(r.get("downloads", 0) for r in rows if not r.get("mirrors", False))
    except Exception:
        result["all_time"] = None

    return result

# ---------------------------------------------------------------------------
# Fly.io log stats
# ---------------------------------------------------------------------------

def get_fly_stats():
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url   = f"https://api.fly.io/v1/apps/{FLY_APP_NAME}/logs?limit=5000&since={since}"
    try:
        _, raw = fetch(url, headers={
            "Authorization": f"Bearer {FLY_API_TOKEN}",
            "Accept":        "application/json",
        })
    except Exception as e:
        return None, str(e)

    stats = {
        "total_calls":   0,
        "total_success": 0,
        "total_errors":  0,
        "total_sats":    0,
        "endpoints":     defaultdict(lambda: {"calls": 0, "success": 0, "errors": 0, "sats": 0, "latencies": []}),
        "recent":        [],
        "first_seen":    None,
        "last_seen":     None,
    }

    for line in raw.splitlines():
        if "BOLTWORK_LOG" not in line:
            continue
        try:
            entry = json.loads(line.split("BOLTWORK_LOG", 1)[1].strip())
        except Exception:
            continue

        endpoint = entry.get("endpoint", "unknown")
        status   = entry.get("status", "")
        ts       = entry.get("ts", "")
        ms       = entry.get("duration_ms", 0)
        sats     = PRICE_MAP.get(endpoint, 0) if status == "success" else 0

        stats["total_calls"] += 1
        ep = stats["endpoints"][endpoint]
        ep["calls"] += 1

        if status == "success":
            stats["total_success"] += 1
            stats["total_sats"]    += sats
            ep["success"] += 1
            ep["sats"]    += sats
        elif status == "error":
            stats["total_errors"] += 1
            ep["errors"] += 1

        if ms:
            ep["latencies"].append(ms)

        if ts:
            if not stats["first_seen"] or ts < stats["first_seen"]:
                stats["first_seen"] = ts
            if not stats["last_seen"] or ts > stats["last_seen"]:
                stats["last_seen"] = ts

        if len(stats["recent"]) < 15:
            stats["recent"].append(entry)

    for ep_data in stats["endpoints"].values():
        lats = ep_data.pop("latencies")
        ep_data["avg_ms"] = int(sum(lats) / len(lats)) if lats else 0

    return stats, None

# ---------------------------------------------------------------------------
# BTC/GBP rate
# ---------------------------------------------------------------------------

def get_btc_gbp():
    try:
        _, body = fetch("https://api.coindesk.com/v1/bpi/currentprice/GBP.json", timeout=5)
        return json.loads(body)["bpi"]["GBP"]["rate_float"]
    except Exception:
        return None

# ---------------------------------------------------------------------------
# API health
# ---------------------------------------------------------------------------

def get_api_health():
    try:
        status, body = fetch(f"{BOLTWORK_API}/health", timeout=10)
        data = json.loads(body)
        return {"ok": True, "version": data.get("version", "?"), "status": status}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------------------
# Build email
# ---------------------------------------------------------------------------

def build_email(now, health, pypi, fly_stats, fly_error, btc_gbp):
    date_str  = now.strftime("%A %d %B %Y")
    time_str  = now.strftime("%H:%M UTC")

    # ── plain text ──────────────────────────────────────────────────────────
    lines = [
        "Boltwork Daily Usage Report",
        f"{date_str}, {time_str}",
        "",
    ]

    # Health
    lines += [
        "── API Health ──────────────────────────────",
        f"{'✓' if health['ok'] else '✗'} parsebit.fly.dev — {'v' + health['version'] + ' online' if health['ok'] else health.get('error', 'unreachable')}",
        "",
    ]

    # PyPI
    lines += ["── PyPI — boltwork-mcp ─────────────────────"]
    if pypi["ok"]:
        lines += [
            f"  All-time  : {pypi.get('all_time', '?')}",
            f"  Last 30d  : {pypi.get('last_month', '?')}",
            f"  Last 7d   : {pypi.get('last_week', '?')}",
            f"  Last 24h  : {pypi.get('last_day', '?')}",
        ]
    else:
        lines.append(f"  Unavailable: {pypi.get('error', '')}")
    lines.append("")

    # Usage
    lines += [f"── API Usage (last {LOOKBACK_HOURS}h) ──────────────────────"]
    if fly_error:
        lines.append(f"  Could not retrieve logs: {fly_error}")
    elif fly_stats["total_calls"] == 0:
        lines.append("  No API calls in this window.")
    else:
        lines += [
            f"  Total calls : {fly_stats['total_calls']}",
            f"  Successful  : {fly_stats['total_success']}",
            f"  Errors      : {fly_stats['total_errors']}",
            f"  Sats earned : {fly_stats['total_sats']:,}",
        ]
        if btc_gbp:
            gbp = (fly_stats["total_sats"] / 100_000_000) * btc_gbp
            lines.append(f"  ≈ GBP value : £{gbp:.4f}  (@ £{btc_gbp:,.0f}/BTC)")
        lines.append("")
        lines.append(f"  {'Endpoint':<26} {'Calls':>5} {'OK':>5} {'Err':>5} {'Sats':>7} {'Avg ms':>7}")
        lines.append(f"  {'─'*26} {'─'*5} {'─'*5} {'─'*5} {'─'*7} {'─'*7}")
        for ep, d in sorted(fly_stats["endpoints"].items(), key=lambda x: -x[1]["calls"]):
            lines.append(f"  {ep:<26} {d['calls']:>5} {d['success']:>5} {d['errors']:>5} {d['sats']:>7,} {d['avg_ms']:>6}ms")

    lines += [
        "",
        "── Links ───────────────────────────────────",
        "Dashboard : crackedminds.co.uk/gateway/dashboard",
        "API       : parsebit.fly.dev",
        "PyPI      : pypistats.org/packages/boltwork-mcp",
        "",
        "Boltwork by Cracked Minds — crackedminds.co.uk",
    ]
    plain = "\n".join(lines)

    # ── HTML ────────────────────────────────────────────────────────────────
    health_colour = "#22c55e" if health["ok"] else "#ef4444"
    health_text   = f"v{health['version']} — online" if health["ok"] else health.get("error", "unreachable")
    health_icon   = "✓" if health["ok"] else "✗"

    # PyPI rows
    if pypi["ok"]:
        pypi_html = f"""
        <table style="width:100%;border-collapse:collapse">
          <tr><td style="padding:4px 0;color:#555;font-size:13px">All-time downloads</td>
              <td style="padding:4px 0;text-align:right;font-weight:600;font-size:13px">{pypi.get('all_time','?')}</td></tr>
          <tr><td style="padding:4px 0;color:#555;font-size:13px">Last 30 days</td>
              <td style="padding:4px 0;text-align:right;font-weight:600;font-size:13px">{pypi.get('last_month','?')}</td></tr>
          <tr><td style="padding:4px 0;color:#555;font-size:13px">Last 7 days</td>
              <td style="padding:4px 0;text-align:right;font-weight:600;font-size:13px">{pypi.get('last_week','?')}</td></tr>
          <tr><td style="padding:4px 0;color:#555;font-size:13px">Last 24 hours</td>
              <td style="padding:4px 0;text-align:right;font-weight:600;font-size:13px">{pypi.get('last_day','?')}</td></tr>
        </table>"""
    else:
        pypi_html = f'<p style="color:#aaa;font-size:13px">Unavailable: {pypi.get("error","")}</p>'

    # Usage section
    if fly_error:
        usage_html = f'<p style="color:#ef4444;font-size:13px">Could not retrieve logs: {fly_error}</p>'
    elif fly_stats["total_calls"] == 0:
        usage_html = '<p style="color:#aaa;font-size:13px">No API calls in this window.</p>'
    else:
        gbp_line = ""
        if btc_gbp:
            gbp = (fly_stats["total_sats"] / 100_000_000) * btc_gbp
            gbp_line = f'<div style="font-size:12px;color:#aaa;margin-top:4px">≈ £{gbp:.4f} GBP @ £{btc_gbp:,.0f}/BTC</div>'

        ep_rows = ""
        for ep, d in sorted(fly_stats["endpoints"].items(), key=lambda x: -x[1]["calls"]):
            ep_rows += f"""
            <tr>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;color:#555;font-family:monospace">{ep}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:center">{d['calls']}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:center;color:#22c55e">{d['success']}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:center;color:{'#ef4444' if d['errors'] > 0 else '#aaa'}">{d['errors']}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:right;color:#f59e0b">{d['sats']:,}</td>
              <td style="padding:6px 10px;border-bottom:1px solid #f0f0f0;font-size:12px;text-align:right;color:#aaa">{d['avg_ms']}ms</td>
            </tr>"""

        usage_html = f"""
        <div style="display:flex;gap:12px;margin-bottom:16px">
          <div style="flex:1;background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:26px;font-weight:700;color:#333">{fly_stats['total_calls']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">TOTAL CALLS</div>
          </div>
          <div style="flex:1;background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:26px;font-weight:700;color:#22c55e">{fly_stats['total_success']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">SUCCESSFUL</div>
          </div>
          <div style="flex:1;background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:26px;font-weight:700;color:#f59e0b">{fly_stats['total_sats']:,}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">SATS EARNED</div>
            {gbp_line}
          </div>
          <div style="flex:1;background:#f9f9f9;border-radius:8px;padding:12px;text-align:center">
            <div style="font-size:26px;font-weight:700;color:{'#ef4444' if fly_stats['total_errors'] > 0 else '#aaa'}">{fly_stats['total_errors']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">ERRORS</div>
          </div>
        </div>
        <table style="width:100%;border-collapse:collapse">
          <thead>
            <tr style="background:#f9f9f9">
              <th style="padding:8px 10px;text-align:left;font-size:11px;color:#888;font-weight:500">Endpoint</th>
              <th style="padding:8px 10px;text-align:center;font-size:11px;color:#888;font-weight:500">Calls</th>
              <th style="padding:8px 10px;text-align:center;font-size:11px;color:#888;font-weight:500">OK</th>
              <th style="padding:8px 10px;text-align:center;font-size:11px;color:#888;font-weight:500">Err</th>
              <th style="padding:8px 10px;text-align:right;font-size:11px;color:#888;font-weight:500">Sats</th>
              <th style="padding:8px 10px;text-align:right;font-size:11px;color:#888;font-weight:500">Avg ms</th>
            </tr>
          </thead>
          <tbody>{ep_rows}</tbody>
        </table>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9f9f9;margin:0;padding:20px">
  <div style="max-width:620px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)">

    <div style="background:#1a1a1a;padding:24px 32px">
      <div style="color:#fff;font-size:20px;font-weight:700">⚡ Boltwork — Daily Report</div>
      <div style="color:rgba(255,255,255,0.6);font-size:13px;margin-top:4px">{date_str} · {time_str}</div>
    </div>

    <div style="padding:24px 32px">

      <!-- Health -->
      <div style="margin-bottom:24px">
        <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:10px">API Health</div>
        <div style="display:flex;align-items:center;gap:10px;padding:12px;background:#f9f9f9;border-radius:8px">
          <span style="color:{health_colour};font-size:18px;font-weight:700">{health_icon}</span>
          <span style="font-size:13px;color:#333">parsebit.fly.dev — {health_text}</span>
        </div>
      </div>

      <!-- PyPI -->
      <div style="margin-bottom:24px">
        <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:10px">PyPI — boltwork-mcp</div>
        {pypi_html}
      </div>

      <!-- Usage -->
      <div style="margin-bottom:24px">
        <div style="font-size:11px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.06em;margin-bottom:10px">API Usage — Last {LOOKBACK_HOURS}h</div>
        {usage_html}
      </div>

      <!-- Footer -->
      <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f0f0f0;font-size:12px;color:#aaa">
        <a href="https://crackedminds.co.uk/gateway/dashboard" style="color:#666;text-decoration:none">Dashboard</a> &nbsp;·&nbsp;
        <a href="https://parsebit.fly.dev" style="color:#666;text-decoration:none">API</a> &nbsp;·&nbsp;
        <a href="https://pypistats.org/packages/boltwork-mcp" style="color:#666;text-decoration:none">PyPI stats</a> &nbsp;·&nbsp;
        <a href="https://crackedminds.co.uk" style="color:#666;text-decoration:none">Cracked Minds</a>
      </div>

    </div>
  </div>
</body></html>"""

    return plain, html

# ---------------------------------------------------------------------------
# Send email
# ---------------------------------------------------------------------------

def send_email(plain, html, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html,  "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print(f"Boltwork usage report — {now.strftime('%d %b %Y %H:%M UTC')}")

    print("Checking API health...")
    health = get_api_health()
    print(f"  {'✓' if health['ok'] else '✗'} {health.get('version', health.get('error'))}")

    print("Fetching PyPI stats...")
    pypi = get_pypi_stats()
    if pypi["ok"]:
        print(f"  All-time: {pypi.get('all_time','?')}  |  Last 30d: {pypi.get('last_month','?')}  |  Last 7d: {pypi.get('last_week','?')}  |  Last 24h: {pypi.get('last_day','?')}")
    else:
        print(f"  Failed: {pypi.get('error')}")

    print(f"Fetching Fly.io logs (last {LOOKBACK_HOURS}h)...")
    fly_stats, fly_error = get_fly_stats()
    if fly_error:
        print(f"  Failed: {fly_error}")
    else:
        print(f"  {fly_stats['total_calls']} calls  |  {fly_stats['total_success']} success  |  {fly_stats['total_sats']:,} sats")

    btc_gbp = get_btc_gbp()

    plain, html = build_email(now, health, pypi, fly_stats or {
        "total_calls": 0, "total_success": 0, "total_errors": 0,
        "total_sats": 0, "endpoints": {}, "recent": [],
        "first_seen": None, "last_seen": None,
    }, fly_error, btc_gbp)

    subject = f"⚡ Boltwork report — {now.strftime('%d %b %Y')} — {fly_stats['total_calls'] if fly_stats else '?'} calls · {fly_stats['total_sats']:,} sats" if fly_stats else f"⚡ Boltwork report — {now.strftime('%d %b %Y')}"

    print(f"Sending email to {NOTIFY_EMAIL}...")
    send_email(plain, html, subject)
    print("Done.")
