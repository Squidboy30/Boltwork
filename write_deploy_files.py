"""
write_deploy_files.py
Run this from inside your summarise-api folder:
    python write_deploy_files.py
"""

from pathlib import Path

FLY_TOML = """\
app = "summarise-api-ian"
primary_region = "lhr"

[build]

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "stop"
  auto_start_machines = true
  min_machines_running = 0

[[vm]]
  memory = "512mb"
  cpu_kind = "shared"
  cpus = 1
"""

DOCKERFILE = """\
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \\
    libpoppler-cpp-dev \\
    poppler-utils \\
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

Path("fly.toml").write_text(FLY_TOML, encoding="utf-8")
print("OK  fly.toml written")

Path("Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
print("OK  Dockerfile written")

print()
print("Both files created. Run: flyctl launch --no-deploy")
