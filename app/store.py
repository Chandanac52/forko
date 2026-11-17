"""Tiny SQLite store so build results survive restarts."""
import json
import os
import sqlite3
from contextlib import closing


def _conn():
    c = sqlite3.connect(os.getenv("FORKO_DB", "forko.db"), timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS builds (id TEXT PRIMARY KEY, repo TEXT, created TEXT, data TEXT NOT NULL)")
    return c


def save(build: dict) -> None:
    key = f"{build['repo']['owner']}/{build['repo']['name']}".lower()
    with closing(_conn()) as c, c:
        c.execute("INSERT OR REPLACE INTO builds VALUES (?,?,?,?)",
                  (build["id"], key, build["created_at"], json.dumps(build)))


def load(build_id: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT data FROM builds WHERE id=?", (build_id,)).fetchone()
    return json.loads(row[0]) if row else None


def latest_for_repo(owner: str, name: str) -> dict | None:
    with closing(_conn()) as c:
        row = c.execute("SELECT data FROM builds WHERE repo=? ORDER BY created DESC LIMIT 1",
                        (f"{owner}/{name}".lower(),)).fetchone()
    return json.loads(row[0]) if row else None
