import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from app import github, main, runner, setup_gen


def test_parse_ok():
    assert github.parse_repo_url("https://github.com/Chandanac52/Agentic_RAG.git") == ("Chandanac52", "Agentic_RAG")
    assert github.parse_repo_url("github.com/a/b/tree/main") == ("a", "b")


@pytest.mark.parametrize("bad", ["https://gitlab.com/a/b", "https://github.com/a", "https://github.com/a/..",
                                 "not a url ; rm -rf /"])
def test_parse_bad(bad):
    with pytest.raises(github.RepoError):
        github.parse_repo_url(bad)


def test_detect_stack():
    assert github.detect_stack({"files": ["requirements.txt"], "language": "Python"}) == "python"
    assert github.detect_stack({"files": ["package.json"], "language": "JavaScript"}) == "node"
    assert github.detect_stack({"files": ["package.json", "setup.py"], "language": "TypeScript"}) == "node"
    assert github.detect_stack({"files": ["README.md"], "language": "Go"}) is None


def test_fix_rules_and_no_repeat():
    f = runner.find_fix("python", "error: pg_config executable not found", set())
    assert f["id"] == "psycopg2-binary"
    assert runner.find_fix("python", "error: pg_config executable not found", {"psycopg2-binary"}) is None
    f = runner.find_fix("python", "ERROR: No matching distribution found for numpy==1.19.0", set())
    assert f["id"] == "unpin-numpy"
    assert runner.find_fix("node", "npm ERR! ERESOLVE unable to resolve", set())["install_override"].endswith("--legacy-peer-deps")
    assert runner.find_fix("python", "all good", set()) is None


@pytest.mark.skipif(not shutil.which("sed"), reason="needs sed")
def test_sed_fixes_really_edit_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0\npsycopg2==2.9.1\nnumpy==1.19.0\nrequests\n")
    for out in ("pg_config executable not found", "No matching distribution found for numpy==1.19.0"):
        subprocess.run(runner.find_fix("python", out, set())["unix"], shell=True, cwd=tmp_path, check=True)
    text = (tmp_path / "requirements.txt").read_text()
    assert "psycopg2-binary==2.9.1" in text and "numpy\n" in text and "flask==2.0" in text


def _build(os_name="windows"):
    return {"id": "abc", "os": os_name, "stack": "python", "runtime": "Python 3.11", "repo": {"owner": "o", "name": "n"},
            "fixes": [{"worked": True, "what_changed": "x", "unix": "UNIXFIX", "windows": "WINFIX"}],
            "install_final": ["pip install -r requirements.txt"], "tests_ran": True, "build_ran": True}


def test_setup_commands_windows_and_linux():
    w = [c["command"] for c in setup_gen.setup_commands(_build("windows"))]
    assert r".venv\Scripts\Activate.ps1" in w and "py -3.11 -m venv .venv" in w and "WINFIX" in w
    u = [c["command"] for c in setup_gen.setup_commands(_build("linux"))]
    assert "source .venv/bin/activate" in u and "UNIXFIX" in u
    b = _build("linux"); b["setup_commands"] = setup_gen.setup_commands(b)
    name, body = setup_gen.render_setup_file(b)
    assert name == "setup.sh" and body.startswith("#!/usr/bin/env bash")


def test_api_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "t.db"))
    monkeypatch.setattr(main, "inspect_repo", lambda o, n: {"owner": o, "name": n, "default_branch": "main",
                                                              "language": "Python", "files": ["requirements.txt"]})

    def fake(build, save):
        build.update(status="verified", steps=[{"name": "Run tests", "status": "passed"}], setup_commands=[{"title": "t", "command": "c"}],
                     setup_file_url="/x", fixes=[], message="ok", finished_at="now", runtime="Python 3.11")
        build["os"] = "linux"
        save(build)
    monkeypatch.setattr(main, "run_build", fake)
    c = TestClient(main.app)
    bid = c.post("/api/builds", json={"repo_url": "https://github.com/a/b", "os": "linux"}).json()["build_id"]
    for _ in range(50):
        r = c.get(f"/api/builds/{bid}").json()
        if r["status"] == "verified":
            break
        time.sleep(0.1)
    assert r["status"] == "verified" and "files" not in r
    assert "setup.sh" in c.get(f"/api/builds/{bid}/setup-file").headers["content-disposition"]
    assert "setup verified" in c.get("/api/badge/a/b.svg").text
    assert c.post("/api/builds", json={"repo_url": "https://example.com/a/b"}).status_code == 400
    assert c.get("/api/builds/nope").status_code == 404
