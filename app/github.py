"""Validate a GitHub URL and inspect the repository through the GitHub API."""
import os
import re
from urllib.parse import urlparse

import httpx

API = "https://api.github.com"
MAX_SIZE_KB = 500 * 1024  # 500 MB
_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class RepoError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.code = code


def parse_repo_url(url: str) -> tuple[str, str]:
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    try:
        p = urlparse(u)
    except ValueError:
        raise RepoError("That does not look like a valid URL.")
    if p.hostname not in ("github.com", "www.github.com"):
        raise RepoError("Only github.com repository URLs are supported.")
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        raise RepoError("URL must look like https://github.com/owner/repo")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    for part in (owner, repo):
        if not _PART.fullmatch(part) or part in (".", ".."):
            raise RepoError("The owner or repository name contains invalid characters.")
    return owner, repo


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "forko-worker"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    return h


def inspect_repo(owner: str, repo: str) -> dict:
    try:
        with httpx.Client(timeout=15, headers=_headers()) as c:
            r = c.get(f"{API}/repos/{owner}/{repo}")
            if r.status_code == 404:
                raise RepoError("Repository not found. Check the URL. Private repositories are not supported.", 404)
            if r.status_code in (403, 429):
                raise RepoError("GitHub API rate limit reached. Please try again later.", 503)
            r.raise_for_status()
            info = r.json()
            if info.get("private"):
                raise RepoError("Private repositories are not supported.", 403)
            if info.get("size", 0) > MAX_SIZE_KB:
                raise RepoError("This repository is too large for Forko today (limit 500 MB).", 413)
            lst = c.get(f"{API}/repos/{owner}/{repo}/contents")
            items = lst.json() if lst.status_code == 200 else []
            files = sorted(i["name"] for i in items if isinstance(i, dict) and i.get("type") == "file")
            dirs = sorted(i["name"] for i in items if isinstance(i, dict) and i.get("type") == "dir")
    except httpx.HTTPError:
        raise RepoError("Could not reach GitHub. Please try again.", 502)
    return {
        "owner": info["owner"]["login"],
        "name": info["name"],
        "default_branch": info.get("default_branch", "main"),
        "language": info.get("language"),
        "files": files,
        "dirs": dirs,
    }


def detect_stack(info: dict) -> str | None:
    files, lang = set(info["files"]), info.get("language") or ""
    py = bool(files & {"requirements.txt", "pyproject.toml", "setup.py"})
    node = "package.json" in files
    if py and node:
        return "node" if lang in ("JavaScript", "TypeScript") else "python"
    return "python" if py else "node" if node else None