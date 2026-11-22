"""Tiny SQLite store so build results survive restarts."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone


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


ACTIVE = ("queued", "analyzing", "building", "testing")


def fail_unfinished(message: str) -> int:
    """Builds still marked active when the worker starts were killed with the old worker."""
    n = 0
    with closing(_conn()) as c, c:
        for bid, data in c.execute("SELECT id, data FROM builds").fetchall():
            b = json.loads(data)
            if b.get("status") not in ACTIVE:
                continue
            b["status"], b["message"] = "failed", message
            b["finished_at"] = datetime.now(timezone.utc).isoformat()
            for st in b.get("steps", []):
                if st.get("status") == "running":
                    st["status"] = "failed"
                    st["log"] = (st.get("log") or "") + "\nInterrupted: the worker stopped during this step."
            c.execute("UPDATE builds SET data=? WHERE id=?", (json.dumps(b), bid))
            n += 1
    return n


def _recent(hours: int) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with closing(_conn()) as c:
        rows = c.execute("SELECT data FROM builds WHERE created >= ?", (cutoff,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def count_recent(owner: str, hours: int = 24) -> int:
    return sum(1 for b in _recent(hours) if b.get("owner") == owner)


def count_active() -> int:
    return sum(1 for b in _recent(3) if b.get("status") in ACTIVE)
