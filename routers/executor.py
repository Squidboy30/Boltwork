import base64, json, os
import httpx
from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

EXECUTOR_SECRET = os.environ.get("EXECUTOR_SECRET", "")
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_API      = "https://api.github.com"


def _auth(secret: str):
    if not EXECUTOR_SECRET:
        raise HTTPException(503, "Executor not configured")
    if secret != EXECUTOR_SECRET:
        raise HTTPException(403, "Invalid executor secret")


# ── Models ────────────────────────────────────────────────────────────────────

class FileWrite(BaseModel):
    repo: str          # e.g. "Squidboy30/cracked-minds"
    path: str          # e.g. "check/index.html"
    content: str       # raw file content (not base64)
    message: str       # commit message
    branch: str = "main"

class FileRead(BaseModel):
    repo: str
    path: str
    branch: str = "main"

class FileList(BaseModel):
    repo: str
    path: str = ""
    branch: str = "main"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _gh_get(path: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{GITHUB_API}/{path}",
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"}
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

async def _gh_put(path: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.put(
            f"{GITHUB_API}/{path}",
            json=payload,
            headers={"Authorization": f"token {GITHUB_TOKEN}",
                     "Accept": "application/vnd.github.v3+json"}
        )
        r.raise_for_status()
        return r.json()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/executor/write")
async def write_file(body: FileWrite, x_executor_secret: str = Header(...)):
    """Write or update a file in a GitHub repo. Used by orchestrator agents."""
    _auth(x_executor_secret)

    # Check if file exists to get SHA for update
    existing = await _gh_get(
        f"repos/{body.repo}/contents/{body.path}?ref={body.branch}"
    )
    sha = existing["sha"] if existing else None

    payload = {
        "message": body.message,
        "content": base64.b64encode(body.content.encode()).decode(),
        "branch": body.branch,
    }
    if sha:
        payload["sha"] = sha

    result = await _gh_put(f"repos/{body.repo}/contents/{body.path}", payload)
    return {
        "ok": True,
        "path": body.path,
        "repo": body.repo,
        "sha": result["content"]["sha"],
        "url": result["content"]["html_url"],
        "action": "updated" if sha else "created",
    }


@router.post("/executor/read")
async def read_file(body: FileRead, x_executor_secret: str = Header(...)):
    """Read a file from a GitHub repo."""
    _auth(x_executor_secret)

    data = await _gh_get(
        f"repos/{body.repo}/contents/{body.path}?ref={body.branch}"
    )
    if not data:
        raise HTTPException(404, f"{body.path} not found in {body.repo}")

    content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    return {
        "ok": True,
        "path": body.path,
        "repo": body.repo,
        "sha": data["sha"],
        "content": content,
    }


@router.post("/executor/list")
async def list_files(body: FileList, x_executor_secret: str = Header(...)):
    """List files in a directory of a GitHub repo."""
    _auth(x_executor_secret)

    path = f"repos/{body.repo}/contents/{body.path}?ref={body.branch}"
    data = await _gh_get(path)
    if not data:
        raise HTTPException(404, f"{body.path} not found in {body.repo}")

    if isinstance(data, list):
        return {
            "ok": True,
            "path": body.path,
            "repo": body.repo,
            "files": [{"name": f["name"], "type": f["type"], "path": f["path"]} for f in data],
        }
    # Single file returned
    return {"ok": True, "path": body.path, "repo": body.repo, "files": [data]}


@router.get("/executor/health")
async def executor_health():
    """Public health check — confirms executor is mounted."""
    return {"ok": True, "executor": "ready",
            "configured": bool(EXECUTOR_SECRET and GITHUB_TOKEN)}
