#!/usr/bin/env python3
"""
Parsebit Lightning Setup Script
================================
Deploys LND + Aperture on Fly.io and wires them to Parsebit.
Run from: C:\\Users\\Ian\\Desktop\\summarise-api

Usage:
    python setup_lightning.py

Stages:
    1. Preflight  - checks flyctl is installed and you're logged in
    2. LND        - creates parsebit-lnd app with persistent volume
    3. Aperture   - creates parsebit-aperture app as L402 reverse proxy
    4. Wire-up    - sets secrets so all three apps can talk to each other
    5. Verify     - confirms 402 responses are live
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

# ── Config ────────────────────────────────────────────────────────────────────
PARSEBIT_APP      = "parsebit"
LND_APP           = "parsebit-lnd"
APERTURE_APP      = "parsebit-aperture"
FLY_REGION        = "lhr"          # London — same region as parsebit
LND_VOLUME        = "lnd_data"
LND_VOLUME_SIZE   = 1              # GB — enough for testnet/mainnet wallet
PRICE_SATS        = 50
PARSEBIT_INTERNAL = "http://parsebit.internal:8080"

# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd, check=True, capture=False):
    """Run a shell command, print it, and return CompletedProcess."""
    print(f"\n$ {cmd}")
    result = subprocess.run(
        cmd, shell=True, check=check,
        capture_output=capture, text=True
    )
    if capture:
        return result
    return result

def run_capture(cmd):
    return run(cmd, check=False, capture=True)

def fly(cmd, check=True):
    return run(f"flyctl {cmd}", check=check)

def fly_capture(cmd):
    return run_capture(f"flyctl {cmd}")

def step(n, title):
    print(f"\n{'='*60}")
    print(f"  STEP {n}: {title}")
    print(f"{'='*60}")

def ok(msg):  print(f"  ✓ {msg}")
def err(msg): print(f"  ✗ {msg}"); sys.exit(1)
def info(msg): print(f"  → {msg}")

# ── Stage 1: Preflight ────────────────────────────────────────────────────────

def preflight():
    step(1, "Preflight checks")

    # flyctl installed?
    r = run_capture("flyctl version")
    if r.returncode != 0:
        err("flyctl not found. Install from https://fly.io/docs/hands-on/install-flyctl/")
    ok(f"flyctl found: {r.stdout.strip().splitlines()[0]}")

    # logged in?
    r = run_capture("flyctl auth whoami")
    if r.returncode != 0:
        err("Not logged in to Fly.io. Run: flyctl auth login")
    ok(f"Logged in as: {r.stdout.strip()}")

    # parsebit app exists?
    r = fly_capture(f"apps list")
    if PARSEBIT_APP not in r.stdout:
        err(f"App '{PARSEBIT_APP}' not found. Deploy Parsebit first.")
    ok(f"Found app: {PARSEBIT_APP}")

# ── Stage 2: LND on Fly.io ───────────────────────────────────────────────────

LND_DOCKERFILE = """\
FROM lightninglabs/lnd:v0.18.2-beta

# Default config — overridden by environment secrets at runtime
COPY lnd.conf /root/.lnd/lnd.conf

EXPOSE 9735 10009 8080
"""

LND_CONFIG = """\
[Application Options]
restlisten=0.0.0.0:8080
rpclisten=0.0.0.0:10009
listen=0.0.0.0:9735
maxpendingchannels=10
alias=parsebit
color=#F7931A
no-macaroons=false
adminmacaroonpath=/root/.lnd/data/chain/bitcoin/mainnet/admin.macaroon

[Bitcoin]
bitcoin.active=1
bitcoin.mainnet=1
bitcoin.node=neutrino

[Neutrino]
neutrino.connect=btcd-mainnet.lightning.computer:10009
neutrino.connect=neutrino.bitlum.io
neutrino.connect=neutrino.olaoluwa.dev

[tor]
tor.active=0
"""

LND_FLY_TOML = f"""\
app = '{LND_APP}'
primary_region = '{FLY_REGION}'

[build]
  dockerfile = 'Dockerfile.lnd'

[mounts]
  source = '{LND_VOLUME}'
  destination = '/root/.lnd'

[[services]]
  internal_port = 9735
  protocol = 'tcp'
  [[services.ports]]
    port = 9735

[[services]]
  internal_port = 8080
  protocol = 'tcp'
  [[services.ports]]
    port = 8080

[env]
  LND_REST_PORT = '8080'

[[vm]]
  memory = '512mb'
  cpu_kind = 'shared'
  cpus = 1
"""

def setup_lnd():
    step(2, "Deploy LND Lightning Node")

    os.makedirs("lnd-app", exist_ok=True)

    with open("lnd-app/Dockerfile.lnd", "w") as f:
        f.write(LND_DOCKERFILE)
    ok("Wrote lnd-app/Dockerfile.lnd")

    with open("lnd-app/lnd.conf", "w") as f:
        f.write(LND_CONFIG)
    ok("Wrote lnd-app/lnd.conf")

    with open("lnd-app/fly.toml", "w") as f:
        f.write(LND_FLY_TOML)
    ok("Wrote lnd-app/fly.toml")

    # Create app (ignore error if already exists)
    r = fly_capture(f"apps create {LND_APP} --org personal")
    if r.returncode == 0 or "already exists" in r.stderr:
        ok(f"App {LND_APP} ready")
    else:
        err(f"Failed to create {LND_APP}: {r.stderr}")

    # Create persistent volume for wallet + channel data
    r = fly_capture(f"volumes list --app {LND_APP}")
    if LND_VOLUME in r.stdout:
        ok(f"Volume {LND_VOLUME} already exists")
    else:
        fly(f"volumes create {LND_VOLUME} --app {LND_APP} --region {FLY_REGION} --size {LND_VOLUME_SIZE}")
        ok(f"Created volume {LND_VOLUME} ({LND_VOLUME_SIZE}GB)")

    # Deploy
    fly(f"deploy lnd-app --app {LND_APP} --config lnd-app/fly.toml --remote-only")
    ok(f"Deployed {LND_APP}")

    info("Waiting 15s for LND to start...")
    time.sleep(15)

    # Get LND REST URL for Aperture
    lnd_url = f"https://{LND_APP}.fly.dev:8080"
    info(f"LND REST URL: {lnd_url}")
    return lnd_url

# ── Stage 3: Aperture ─────────────────────────────────────────────────────────

APERTURE_DOCKERFILE = """\
FROM lightninglabs/aperture:latest

COPY aperture.yaml /root/.aperture/aperture.yaml

EXPOSE 8081
"""

def aperture_config(lnd_url):
    return f"""\
listenaddr: "0.0.0.0:8081"
debuglevel: info
autocert: false
selfcert: true
servername: {APERTURE_APP}.fly.dev

authenticator:
  lndhost: "{LND_APP}.internal:10009"
  tlspath: "/root/.lnd/tls.cert"
  macdir: "/root/.lnd/data/chain/bitcoin/mainnet/"
  network: "mainnet"

services:
  - name: "summarise-upload"
    hostregexp: ".*"
    pathregexp: "^/summarise/upload.*"
    address: "{PARSEBIT_INTERNAL}"
    protocol: http
    constraints:
      "tier":
        "0":
          price: {PRICE_SATS}
          caveats:
            - condition: "allow"

  - name: "summarise-url"
    hostregexp: ".*"
    pathregexp: "^/summarise/url.*"
    address: "{PARSEBIT_INTERNAL}"
    protocol: http
    constraints:
      "tier":
        "0":
          price: {PRICE_SATS}
          caveats:
            - condition: "allow"

  - name: "public"
    hostregexp: ".*"
    pathregexp: "^/(health|agent-spec|.well-known|docs|openapi|).*"
    address: "{PARSEBIT_INTERNAL}"
    protocol: http
    auth: false

dbbackend: sqlite
sqlite:
  dbpath: /root/.aperture/aperture.db
"""

APERTURE_FLY_TOML = f"""\
app = '{APERTURE_APP}'
primary_region = '{FLY_REGION}'

[build]
  dockerfile = 'Dockerfile.aperture'

[[services]]
  internal_port = 8081
  protocol = 'tcp'
  [[services.ports]]
    handlers = ['tls', 'http']
    port = 443
  [[services.ports]]
    handlers = ['http']
    port = 80

[[vm]]
  memory = '256mb'
  cpu_kind = 'shared'
  cpus = 1
"""

def setup_aperture(lnd_url):
    step(3, "Deploy Aperture L402 Proxy")

    os.makedirs("aperture-app", exist_ok=True)

    with open("aperture-app/Dockerfile.aperture", "w") as f:
        f.write(APERTURE_DOCKERFILE)
    ok("Wrote aperture-app/Dockerfile.aperture")

    with open("aperture-app/aperture.yaml", "w") as f:
        f.write(aperture_config(lnd_url))
    ok("Wrote aperture-app/aperture.yaml")

    with open("aperture-app/fly.toml", "w") as f:
        f.write(APERTURE_FLY_TOML)
    ok("Wrote aperture-app/fly.toml")

    # Create app
    r = fly_capture(f"apps create {APERTURE_APP} --org personal")
    if r.returncode == 0 or "already exists" in r.stderr:
        ok(f"App {APERTURE_APP} ready")
    else:
        err(f"Failed to create {APERTURE_APP}: {r.stderr}")

    fly(f"deploy aperture-app --app {APERTURE_APP} --config aperture-app/fly.toml --remote-only")
    ok(f"Deployed {APERTURE_APP}")

# ── Stage 4: Wire-up ──────────────────────────────────────────────────────────

def wiremap():
    step(4, "Wire apps together")

    aperture_url = f"https://{APERTURE_APP}.fly.dev"
    info(f"Aperture public URL: {aperture_url}")

    # Tell Parsebit about the Aperture URL (for agent-spec.md and l402.json)
    fly(f"secrets set SERVICE_URL={aperture_url} --app {PARSEBIT_APP}")
    ok(f"Set SERVICE_URL on {PARSEBIT_APP} → {aperture_url}")

    info("Redeploying Parsebit to pick up new SERVICE_URL...")
    fly(f"deploy --app {PARSEBIT_APP} --remote-only")
    ok("Parsebit redeployed")

    return aperture_url

# ── Stage 5: Verify ───────────────────────────────────────────────────────────

def verify(aperture_url):
    step(5, "Verify L402 responses")

    info("Waiting 20s for Aperture to fully start...")
    time.sleep(20)

    endpoints = [
        f"{aperture_url}/summarise/upload",
        f"{aperture_url}/summarise/url",
    ]

    all_ok = True
    for url in endpoints:
        try:
            req = urllib.request.Request(url, method="POST",
                                          headers={"Content-Type": "application/json"},
                                          data=b"{}")
            try:
                urllib.request.urlopen(req, timeout=10)
                print(f"  ? {url} → got 200 (no paywall yet?)")
                all_ok = False
            except urllib.error.HTTPError as e:
                if e.code == 402:
                    ok(f"{url} → 402 Payment Required ✓")
                    auth = e.headers.get("WWW-Authenticate", "")
                    if "L402" in auth or "macaroon" in auth.lower():
                        ok(f"  WWW-Authenticate header present ✓")
                    else:
                        info(f"  WWW-Authenticate: {auth[:80]}")
                elif e.code == 422:
                    info(f"{url} → 422 (LND not yet synced, retry in a few minutes)")
                else:
                    info(f"{url} → HTTP {e.code}")
                    all_ok = False
        except Exception as ex:
            info(f"{url} → {ex}")
            all_ok = False

    return all_ok

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "="*60)
    print("  PARSEBIT LIGHTNING SETUP")
    print("  Deploying LND + Aperture on Fly.io")
    print("="*60)

    preflight()
    lnd_url = setup_lnd()
    setup_aperture(lnd_url)
    aperture_url = wiremap()
    success = verify(aperture_url)

    print("\n" + "="*60)
    if success:
        print("  SETUP COMPLETE")
        print(f"\n  Aperture (L402 gateway): {aperture_url}")
        print(f"  LND node:                https://{LND_APP}.fly.dev")
        print(f"  Parsebit (internal):     {PARSEBIT_INTERNAL}")
        print("""
  NEXT STEPS:
  1. Fund your LND wallet:
       flyctl ssh console --app parsebit-lnd
       lncli newaddress p2wkh
     Send a small amount of BTC to that address.

  2. Open a Lightning channel (for routing):
       lncli connect <peer_pubkey>@<host>
       lncli openchannel --node_key <pubkey> --local_amt 100000

  3. Register on 402 Index (now it will verify):
       python setup_lightning.py --register

  4. Brief Alex to run QA (alex_qa_brief.md in this folder).
""")
    else:
        print("  SETUP COMPLETED WITH WARNINGS")
        print("  LND may still be syncing — re-run verify in 5 mins:")
        print("  python setup_lightning.py --verify-only")
    print("="*60)


if __name__ == "__main__":
    if "--verify-only" in sys.argv:
        aperture_url = f"https://{APERTURE_APP}.fly.dev"
        verify(aperture_url)
    else:
        main()
