"""
Boltwork Daily Health Monitor
==============================
Checks all Boltwork services and sends a daily email report.
Runs via GitHub Actions every morning at 8am UK time.
"""

import os
import sys
import json
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

GMAIL_USER     = os.environ["GMAIL_USER"]
GMAIL_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL   = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)
FLY_API_TOKEN  = os.environ.get("FLY_API_TOKEN", "")

BOLTWORK_API  = "https://parsebit.fly.dev"
BOLTWORK_L402 = "https://parsebit-lnd.fly.dev"
FLY_APP_NAME  = "parsebit"

TIMEOUT = 15


def check(label, url, method="GET", body=None, headers=None, expected_status=None):
    try:
        req = urllib.request.Request(url, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        if body:
            req.data = body.encode() if isinstance(body, str) else body
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            status = r.status
            try:
                data = json.loads(r.read())
                detail = json.dumps(data)[:200]
            except Exception:
                detail = "OK"
            ok = True if expected_status is None else status == expected_status
            return ok, status, detail
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            detail = e.read().decode()[:200]
        except Exception:
            detail = str(e)
        ok = True if expected_status is not None and status == expected_status else False
        return ok, status, detail
    except Exception as e:
        return False, 0, str(e)[:200]


def check_402(label, url, body):
    ok, status, detail = check(
        label, url, method="POST",
        body=body,
        headers={"Content-Type": "application/json"},
        expected_status=402
    )
    return ok, status, "Lightning invoice issued (402 Payment Required)" if ok else detail


def get_usage_stats():
    stats = {
        "total_calls": 0,
        "total_sats": 0,
        "endpoints": {},
        "errors": 0,
        "available": False
    }

    if not FLY_API_TOKEN:
        return stats

    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"https://api.fly.io/v1/apps/{FLY_APP_NAME}/logs?limit=2000&since={since}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {FLY_API_TOKEN}")
        req.add_header("Accept", "application/json")

        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()

        price_map = {
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
            "/memory/store": 10,
            "/memory/retrieve": 5,
            "/memory/delete": 0,
            "/trial/review": 0,
            "/trial/summarise": 0,
            "/workflow/run": 1000,
        }

        for line in raw.strip().split("\n"):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                msg = entry.get("message", "") or entry.get("text", "")
                if "BOLTWORK_LOG" not in msg:
                    continue
                log_json = msg.split("BOLTWORK_LOG", 1)[1].strip()
                log = json.loads(log_json)
                endpoint = log.get("endpoint", "unknown")
                status = log.get("status", "")
                stats["total_calls"] += 1
                if status == "error":
                    stats["errors"] += 1
                sats = price_map.get(endpoint, 0)
                if status == "success":
                    stats["total_sats"] += sats
                if endpoint not in stats["endpoints"]:
                    stats["endpoints"][endpoint] = {"calls": 0, "success": 0, "sats": 0}
                stats["endpoints"][endpoint]["calls"] += 1
                if status == "success":
                    stats["endpoints"][endpoint]["success"] += 1
                    stats["endpoints"][endpoint]["sats"] += sats
            except Exception:
                continue

        stats["available"] = True

    except Exception as e:
        stats["error_msg"] = str(e)[:100]

    return stats


def run_checks():
    now = datetime.now(timezone.utc)
    results = []

    # 1. API health
    ok, status, detail = check("Health", f"{BOLTWORK_API}/health", expected_status=200)
    version = "unknown"
    try:
        data = json.loads(detail)
        version = data.get("version", "unknown")
    except Exception:
        pass
    results.append({
        "name": "Boltwork API",
        "ok": ok, "status": status,
        "detail": f"v{version} — online" if ok else detail
    })

    # 2. Agent discovery — re-fetch full content to count endpoints
    ok, status, detail = check("L402 discovery", f"{BOLTWORK_API}/.well-known/l402.json", expected_status=200)
    endpoint_count = 0
    try:
        req = urllib.request.Request(f"{BOLTWORK_API}/.well-known/l402.json")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            full_data = json.loads(r.read())
            endpoint_count = len(full_data.get("pricing", []))
    except Exception:
        pass
    results.append({
        "name": "Agent discovery (l402.json)",
        "ok": ok, "status": status,
        "detail": f"{endpoint_count} endpoints advertised" if ok else detail
    })

    # 3. Agent spec
    ok, status, detail = check("Agent spec", f"{BOLTWORK_API}/agent-spec.md", expected_status=200)
    results.append({
        "name": "Agent spec (/agent-spec.md)",
        "ok": ok, "status": status,
        "detail": "Accessible" if ok else detail
    })

    # 4. FastAPI route checks — empty body triggers 422 if route exists
    routes = [
        ("FastAPI route — /summarise/url", f"{BOLTWORK_API}/summarise/url", '{}', [422]),
        ("FastAPI route — /review/code", f"{BOLTWORK_API}/review/code", '{}', [422]),
        ("FastAPI route — /extract/webpage", f"{BOLTWORK_API}/extract/webpage", '{}', [422]),
        ("FastAPI route — /extract/data", f"{BOLTWORK_API}/extract/data", '{}', [422]),
        ("FastAPI route — /translate", f"{BOLTWORK_API}/translate", '{}', [422]),
        ("FastAPI route — /analyse/tables", f"{BOLTWORK_API}/analyse/tables", '{}', [422]),
        ("FastAPI route — /analyse/compare", f"{BOLTWORK_API}/analyse/compare", '{}', [422]),
        ("FastAPI route — /analyse/explain", f"{BOLTWORK_API}/analyse/explain", '{}', [422]),
        ("FastAPI route — /trial/review", f"{BOLTWORK_API}/trial/review", '{}', [422]),
        ("FastAPI route — /trial/summarise", f"{BOLTWORK_API}/trial/summarise", '{}', [422]),
        ("FastAPI route — /memory/store", f"{BOLTWORK_API}/memory/store", '{}', [422]),
        ("FastAPI route — /memory/retrieve", f"{BOLTWORK_API}/memory/retrieve", '{}', [422]),
        ("FastAPI route — /memory/delete", f"{BOLTWORK_API}/memory/delete", '{}', [422]),
        ("FastAPI route — /workflow/run", f"{BOLTWORK_API}/workflow/run", '{}', [422]),
    ]
    for name, url, body, expected in routes:
        ok, status, detail = check(name, url, method="POST",
            body=body, headers={"Content-Type": "application/json"})
        ok = status in expected
        detail = f"Route reachable (HTTP {status})" if ok else f"HTTP {status} — unexpected: {detail[:80]}"
        results.append({"name": name, "ok": ok, "status": status, "detail": detail})

    # 5-11. Lightning gates (Aperture 402 checks)
    gates = [
        ("Lightning gate — /summarise/upload", f"{BOLTWORK_L402}/summarise/upload", "{}"),
        ("Lightning gate — /summarise/url", f"{BOLTWORK_L402}/summarise/url", '{"url":"https://example.com/test.pdf"}'),
        ("Lightning gate — /review/code (2000 sats)", f"{BOLTWORK_L402}/review/code", '{"code":"def hello(): pass"}'),
        ("Lightning gate — /review/url (2000 sats)", f"{BOLTWORK_L402}/review/url", '{"url":"https://github.com/Squidboy30/Boltwork/blob/main/main.py"}'),
        ("Lightning gate — /extract/webpage (100 sats)", f"{BOLTWORK_L402}/extract/webpage", '{"url":"https://example.com"}'),
        ("Lightning gate — /extract/data (200 sats)", f"{BOLTWORK_L402}/extract/data", '{"url":"https://example.com/test.pdf"}'),
        ("Lightning gate — /translate (150 sats)", f"{BOLTWORK_L402}/translate", '{"text":"hello world","target_language":"spanish"}'),
        ("Lightning gate — /analyse/tables (300 sats)", f"{BOLTWORK_L402}/analyse/tables", '{"url":"https://example.com/test.pdf"}'),
        ("Lightning gate — /analyse/compare (500 sats)", f"{BOLTWORK_L402}/analyse/compare", '{"url_a":"https://example.com/a.pdf","url_b":"https://example.com/b.pdf"}'),
        ("Lightning gate — /analyse/explain (500 sats)", f"{BOLTWORK_L402}/analyse/explain", '{"code":"def hello(): pass"}'),
        ("Lightning gate — /memory/store (10 sats)", f"{BOLTWORK_L402}/memory/store", '{"agent_id":"healthcheck","entries":{"ping":"pong"}}'),
        ("Lightning gate — /memory/retrieve (5 sats)", f"{BOLTWORK_L402}/memory/retrieve", '{"agent_id":"healthcheck"}'),
        ("Lightning gate — /workflow/run (1000 sats)", f"{BOLTWORK_L402}/workflow/run", '{"steps":[{"service":"webpage","input":{"url":"https://example.com"}}]}'),
    ]
    for name, url, body in gates:
        ok, status, detail = check_402(name, url, body)
        results.append({"name": name, "ok": ok, "status": status, "detail": detail})

    # Trial endpoints — price 0, Aperture passes through, no 402 expected.
    # Empty body → 422 confirms the route is live. Real body → 200 confirms Claude responds.
    trial_checks = [
        ("Trial endpoint — /trial/info (free)",
         f"{BOLTWORK_API}/trial/info", None, [200]),
        ("Trial endpoint — /trial/review (free, empty body)",
         f"{BOLTWORK_API}/trial/review", '{}', [422]),
        ("Trial endpoint — /trial/summarise (free, real call)",
         f"{BOLTWORK_API}/trial/summarise", '{"text":"Bitcoin is a decentralised digital currency."}', [200]),
    ]
    for name, url, body, expected in trial_checks:
        if body:
            ok, status, detail = check(name, url, method="POST",
                body=body, headers={"Content-Type": "application/json"})
        else:
            ok, status, detail = check(name, url, expected_status=expected[0])
        ok = status in expected
        detail = f"OK (HTTP {status})" if ok else f"HTTP {status} — unexpected: {detail[:80]}"
        results.append({"name": name, "ok": ok, "status": status, "detail": detail})

    usage = get_usage_stats()
    all_ok = all(r["ok"] for r in results)
    return now, results, all_ok, usage


def build_email(now, results, all_ok, usage):
    total   = len(results)
    passing = sum(1 for r in results if r["ok"])
    failing = total - passing

    status_emoji = "✅" if all_ok else "🚨"
    status_text  = "ALL SYSTEMS OPERATIONAL" if all_ok else f"{failing} SERVICE(S) DOWN"
    date_str     = now.strftime("%A %d %B %Y, %H:%M UTC")

    usage_lines = ["", "--- Usage (Last 24h) ---"]
    if usage["available"]:
        usage_lines.append(f"Total API calls: {usage['total_calls']}")
        usage_lines.append(f"Total sats earned: {usage['total_sats']} sats")
        usage_lines.append(f"Errors: {usage['errors']}")
        if usage["endpoints"]:
            usage_lines.append("")
            for ep, data in sorted(usage["endpoints"].items()):
                usage_lines.append(f"  {ep}: {data['calls']} calls, {data['success']} success, {data['sats']} sats")
    else:
        usage_lines.append("Usage data unavailable (check FLY_API_TOKEN secret)")

    lines = [
        "Boltwork Daily Health Report",
        date_str, "",
        f"Status: {status_text}",
        f"Checks: {passing}/{total} passing",
    ] + usage_lines + [
        "",
        "--- Service Checks ---",
    ]
    for r in results:
        icon = "✓" if r["ok"] else "✗"
        lines.append(f"{icon} {r['name']}")
        lines.append(f"  HTTP {r['status']} — {r['detail']}")
        lines.append("")
    lines += [
        "--- Links ---",
        f"API: {BOLTWORK_API}",
        f"L402 Gateway: {BOLTWORK_L402}",
        "GitHub: https://github.com/Squidboy30/Boltwork",
        "402 Index: https://402index.io",
        "",
        "Boltwork by Cracked Minds — crackedminds.co.uk",
    ]
    plain = "\n".join(lines)

    if usage["available"]:
        ep_rows = ""
        for ep, data in sorted(usage["endpoints"].items()):
            ep_rows += f"""
            <tr>
              <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;color:#555">{ep}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;text-align:center">{data['calls']}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;text-align:center;color:#22c55e">{data['success']}</td>
              <td style="padding:6px 12px;border-bottom:1px solid #f0f0f0;font-size:13px;text-align:right;color:#f59e0b">{data['sats']} sats</td>
            </tr>"""

        usage_html = f"""
      <div style="margin-bottom:24px">
        <div style="font-size:13px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:12px">Usage — Last 24h</div>
        <div style="display:flex;gap:16px;margin-bottom:16px">
          <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:12px">
            <div style="font-size:28px;font-weight:700;color:#333">{usage['total_calls']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">API CALLS</div>
          </div>
          <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:12px">
            <div style="font-size:28px;font-weight:700;color:#f59e0b">{usage['total_sats']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">SATS EARNED</div>
          </div>
          <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:12px">
            <div style="font-size:28px;font-weight:700;color:{'#ef4444' if usage['errors'] > 0 else '#888'}">{usage['errors']}</div>
            <div style="font-size:11px;color:#888;margin-top:2px">ERRORS</div>
          </div>
        </div>
        {'<table style="width:100%;border-collapse:collapse"><thead><tr style="background:#f9f9f9"><th style="padding:8px 12px;text-align:left;font-size:11px;color:#888;font-weight:500">Endpoint</th><th style="padding:8px 12px;text-align:center;font-size:11px;color:#888;font-weight:500">Calls</th><th style="padding:8px 12px;text-align:center;font-size:11px;color:#888;font-weight:500">Success</th><th style="padding:8px 12px;text-align:right;font-size:11px;color:#888;font-weight:500">Sats</th></tr></thead><tbody>' + ep_rows + '</tbody></table>' if ep_rows else '<div style="font-size:13px;color:#aaa;padding:8px 0">No API calls in the last 24 hours.</div>'}
      </div>"""
    else:
        usage_html = """
      <div style="margin-bottom:24px;padding:12px;background:#f9f9f9;border-radius:8px">
        <div style="font-size:13px;color:#aaa">Usage data unavailable — check FLY_API_TOKEN secret.</div>
      </div>"""

    rows = ""
    for r in results:
        colour = "#22c55e" if r["ok"] else "#ef4444"
        icon   = "✓" if r["ok"] else "✗"
        rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0">
            <span style="color:{colour};font-weight:700;font-size:16px">{icon}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;font-weight:500">{r['name']}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #f0f0f0;color:#666;font-size:13px">
            HTTP {r['status']} — {r['detail']}
          </td>
        </tr>"""

    banner = "#22c55e" if all_ok else "#ef4444"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9f9f9;margin:0;padding:20px">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08)">
    <div style="background:{banner};padding:24px 32px">
      <div style="color:#fff;font-size:22px;font-weight:700">{status_emoji} Boltwork Health Report</div>
      <div style="color:rgba(255,255,255,0.85);font-size:14px;margin-top:4px">{date_str}</div>
    </div>
    <div style="padding:24px 32px">
      <div style="display:flex;gap:24px;margin-bottom:24px">
        <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:16px">
          <div style="font-size:32px;font-weight:700;color:#22c55e">{passing}</div>
          <div style="font-size:12px;color:#888;margin-top:4px">PASSING</div>
        </div>
        <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:16px">
          <div style="font-size:32px;font-weight:700;color:{'#ef4444' if failing > 0 else '#888'}">{failing}</div>
          <div style="font-size:12px;color:#888;margin-top:4px">FAILING</div>
        </div>
        <div style="text-align:center;flex:1;background:#f9f9f9;border-radius:8px;padding:16px">
          <div style="font-size:32px;font-weight:700;color:#333">{total}</div>
          <div style="font-size:12px;color:#888;margin-top:4px">TOTAL CHECKS</div>
        </div>
      </div>
      {usage_html}
      <div style="font-size:13px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:12px">Service Checks</div>
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="background:#f9f9f9">
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;font-weight:500"></th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;font-weight:500;text-transform:uppercase">Service</th>
          <th style="padding:10px 12px;text-align:left;font-size:12px;color:#888;font-weight:500;text-transform:uppercase">Detail</th>
        </tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <div style="margin-top:24px;padding-top:16px;border-top:1px solid #f0f0f0;font-size:12px;color:#aaa">
        <a href="{BOLTWORK_API}" style="color:#666;text-decoration:none">Boltwork API</a> &nbsp;·&nbsp;
        <a href="https://github.com/Squidboy30/Boltwork" style="color:#666;text-decoration:none">GitHub</a> &nbsp;·&nbsp;
        <a href="https://402index.io" style="color:#666;text-decoration:none">402 Index</a> &nbsp;·&nbsp;
        <a href="https://crackedminds.co.uk" style="color:#666;text-decoration:none">Cracked Minds</a>
      </div>
    </div>
  </div>
</body></html>"""

    return plain, html, status_text


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


if __name__ == "__main__":
    print("Running Boltwork health checks...")
    now, results, all_ok, usage = run_checks()
    for r in results:
        icon = "✓" if r["ok"] else "✗"
        print(f"  {icon} {r['name']}: HTTP {r['status']} — {r['detail'][:80]}")
    print(f"\nUsage stats: {usage['total_calls']} calls, {usage['total_sats']} sats earned")
    plain, html, status_text = build_email(now, results, all_ok, usage)
    subject = f"{'✅' if all_ok else '🚨'} Boltwork — {status_text} — {now.strftime('%d %b %Y')}"
    print(f"\nSending: {subject}")
    send_email(plain, html, subject)
    print("Email sent.")
    sys.exit(0 if all_ok else 1)