"""
fix_flydeploy.py
Run from inside your summarise-api folder:
    python fix_flydeploy.py
"""

from pathlib import Path

FLY_TOML = 'app = "parsebit"\nprimary_region = "lhr"\n\n[build]\n\n[http_service]\n  internal_port = 8000\n  force_https = true\n  auto_stop_machines = "stop"\n  auto_start_machines = true\n  min_machines_running = 0\n\n[dnsconfig]\n  nameservers = ["8.8.8.8", "8.8.4.4"]\n\n[[vm]]\n  memory = "512mb"\n  cpu_kind = "shared"\n  cpus = 1\n'

Path("fly.toml").write_text(FLY_TOML, encoding="utf-8")
print("OK  fly.toml rewritten cleanly")
print()
print("Now run: flyctl deploy")
