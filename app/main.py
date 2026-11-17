"""Forko worker API. Run: uvicorn app.main:app --port 8000"""
import os
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from . import store
from .github import RepoError, detect_stack, inspect_repo, parse_repo_url
from .runner import run_build
from .setup_gen import render_setup_file

app = FastAPI(title="Forko worker", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in os.getenv("FORKO_CORS_ORIGINS", "*").split(",")],
                   allow_methods=["GET", "POST"], allow_headers=["*"])
pool = ThreadPoolExecutor(max_workers=int(os.getenv("FORKO_WORKERS", "2")))
HITS: dict[str, deque] = defaultdict(deque)
PER_HOUR = int(os.getenv("FORKO_BUILDS_PER_HOUR", "10"))
PUBLIC = ("id", "status", "repo", "os", "stack", "runtime", "steps", "fixes", "setup_commands", "setup_file_url",
          "verified_on", "note", "message", "created_at", "finished_at")


class BuildRequest(BaseModel):
    repo_url: str
    os: Literal["windows", "macos", "linux"] = "linux"


def _rate_limit(ip: str):
    now, q = time.time(), HITS[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= PER_HOUR:
        raise HTTPException(429, "Too many builds from this address. Please try again later.")
    q.append(now)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/api/builds")
def create_build(req: BuildRequest, request: Request):
    _rate_limit(request.client.host if request.client else "unknown")
    try:
        owner, name = parse_repo_url(req.repo_url)
        info = inspect_repo(owner, name)
    except RepoError as e:
        raise HTTPException(e.code, str(e))
    stack = detect_stack(info)
    if not stack:
        raise HTTPException(422, "Forko supports Python and Node.js repositories today. No requirements.txt, "
                                 "pyproject.toml, setup.py or package.json was found in the repository root.")
    build = {"id": str(uuid.uuid4()), "status": "queued", "os": req.os, "stack": stack, "files": info["files"],
             "repo": {"owner": info["owner"], "name": info["name"], "branch": info["default_branch"], "language": info["language"]},
             "steps": [], "fixes": [], "setup_commands": [], "setup_file_url": None, "verified_on": None,
             "note": None, "message": None, "created_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
    store.save(build)
    pool.submit(run_build, build, store.save)
    return {"build_id": build["id"]}


@app.get("/api/builds/{build_id}")
def get_build(build_id: str):
    b = store.load(build_id)
    if not b:
        raise HTTPException(404, "Build not found.")
    return {k: b.get(k) for k in PUBLIC}


@app.get("/api/builds/{build_id}/setup-file")
def setup_file(build_id: str):
    b = store.load(build_id)
    if not b or not b.get("setup_commands"):
        raise HTTPException(404, "No setup file for this build.")
    fname, body = render_setup_file(b)
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/badge/{owner}/{name}.svg")
def badge(owner: str, name: str):
    b = store.latest_for_repo(owner, name)
    text, color = {"verified": ("setup verified", "#2ea043"), "partially_verified": ("partially verified", "#d29922"),
                   "failed": ("setup failing", "#cf222e")}.get(b["status"] if b else "", ("not checked", "#6e7681"))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="170" height="20"><rect width="56" height="20" fill="#555"/>'
           f'<rect x="56" width="114" height="20" fill="{color}"/><g fill="#fff" font-family="Verdana,sans-serif" font-size="11">'
           f'<text x="8" y="14">forko</text><text x="64" y="14">{text}</text></g></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=300"})
