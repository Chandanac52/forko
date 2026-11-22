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
    assert "psycopg2-binary\n" in text and "psycopg2-binary==" not in text and "numpy\n" in text and "flask==2.0" in text


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
    assert "charset=utf-8" in c.get(f"/api/builds/{bid}").headers["content-type"]
    assert "setup.sh" in c.get(f"/api/builds/{bid}/setup-file").headers["content-disposition"]
    assert "setup verified" in c.get("/api/badge/a/b.svg").text
    assert c.post("/api/builds", json={"repo_url": "https://example.com/a/b"}).status_code == 400
    assert c.get("/api/builds/nope").status_code == 404


def test_log_clip_keeps_head_and_tail():
    text = "HEAD" + "x" * 50000 + "TAIL"
    out = runner._clip(text)
    assert out.startswith("HEAD") and out.endswith("TAIL") and len(out) < 21000
    assert runner._clip("short") == "short"


def test_fail_unfinished(monkeypatch, tmp_path):
    from app import store
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "u.db"))
    base = {"repo": {"owner": "a", "name": "b"}, "created_at": "2026-01-01T00:00:00+00:00"}
    store.save({**base, "id": "1", "status": "building", "steps": [{"name": "Install dependencies", "status": "running", "log": ""}]})
    store.save({**base, "id": "2", "status": "verified", "steps": []})
    assert store.fail_unfinished("restarted") == 1
    one, two = store.load("1"), store.load("2")
    assert one["status"] == "failed" and one["steps"][0]["status"] == "failed" and one["message"] == "restarted"
    assert two["status"] == "verified"


def test_pick_python(monkeypatch):
    monkeypatch.setattr(runner, "_image_exists", lambda image: True)
    msg = "ERROR: Package 'x' requires a different Python: 3.11.18 not in '>=3.12'"
    assert runner.pick_python(msg, ["3.11"]) == ("3.12", ">=3.12")
    assert runner.pick_python(msg.replace(">=3.12", ">=3.13.0"), ["3.11"])[0] == "3.13"
    assert runner.pick_python(msg, ["3.11", "3.12", "3.13"]) is None
    assert runner.pick_python("some other error", ["3.11"]) is None
    monkeypatch.setattr(runner, "_image_exists", lambda image: False)
    assert runner.pick_python(msg, ["3.11"]) is None


class FakeSandbox:
    def __init__(self, image, name):
        self.image = image

    def start(self): pass
    def destroy(self): pass

    def exec(self, script, timeout):
        if "pip install -e ." in script or "pip install -r" in script:
            if self.image.endswith("3.11"):
                return 1, "ERROR: Package 'p' requires a different Python: 3.11.18 not in '>=3.12'"
            return 0, "installed"
        if "package.json" in script:
            return 1, ""
        if "pytest" in script:
            return 0, "1 passed"
        return 0, "ok"


def test_python_version_retry(monkeypatch):
    monkeypatch.setattr(runner, "Sandbox", FakeSandbox)
    monkeypatch.setattr(runner, "_image_exists", lambda image: True)
    b = {"id": "abcdef123456", "os": "linux", "stack": "python", "files": ["pyproject.toml"],
         "repo": {"owner": "o", "name": "n"}}
    runner.run_build(b, lambda build: None)
    assert b["status"] == "verified"
    assert b["runtime"] == "Python 3.12"
    assert [f["id"] for f in b["fixes"]] == ["python-3.12"] and b["fixes"][0]["worked"] is True and b["fixes"][0]["cleared"] is True
    assert [s["name"] for s in b["steps"]][0] == "Detect stack" and all(s["status"] != "failed" for s in b["steps"])
    assert any("python3.12 -m venv" in c["command"] for c in b["setup_commands"])


GYM_ERR = ("error in gym setup command: 'extras_require' must be a dictionary whose values are strings or lists of "
           "strings containing valid project/version requirement specifiers.\nERROR: Failed to build 'gym'")


def test_legacy_build_tools_fix_rule_and_setup_order():
    f = runner.find_fix("python", GYM_ERR, set())
    assert f["id"] == "legacy-build-tools" and f["phase"] == "after_venv" and "PIP_CONSTRAINT" in f["unix"]
    assert runner.find_fix("python", GYM_ERR, {"legacy-build-tools"}) is None
    b = _build("linux")
    b["fixes"] = [{**f, "worked": True}]
    cmds = [c["command"] for c in setup_gen.setup_commands(b)]
    assert cmds.index("source .venv/bin/activate") < next(i for i, c in enumerate(cmds) if "PIP_CONSTRAINT" in c) < cmds.index("pip install -r requirements.txt")


def test_install_switches_to_legacy_tools_after_gym_error():
    scripts = []

    class Sb:
        def exec(self, script, timeout):
            scripts.append(script)
            return (1, GYM_ERR) if len(scripts) == 1 else (0, "ok")

    ok, out, cmds, fixes = runner._install(Sb(), "python", {"requirements.txt"})
    assert ok and fixes[0]["id"] == "legacy-build-tools" and fixes[0]["worked"] is True
    assert "PIP_CONSTRAINT" not in scripts[0] and "PIP_CONSTRAINT" in scripts[1] and "--upgrade pip" not in scripts[1]


def test_fix_marked_cleared_when_a_different_error_follows():
    outs = iter([(1, GYM_ERR), (1, "ERROR: Could not install packages due to an OSError: [Errno 28] No space left on device")])

    class Sb:
        def exec(self, script, timeout):
            return next(outs)

    ok, out, cmds, fixes = runner._install(Sb(), "python", {"requirements.txt"})
    assert not ok and fixes[0]["worked"] is False and fixes[0]["cleared"] is True


def test_fail_message_for_sandbox_limit():
    b = {}
    runner._fail(b, "Install dependencies", "OSError: [Errno 28] No space left on device")
    assert b["status"] == "failed" and "sandbox" in b["message"]
    runner._fail(b, "Build")
    assert b["message"].startswith("Failed at: Build")


@pytest.mark.skipif(not shutil.which("sed"), reason="needs sed")
def test_psycopg_fix_handles_pinned_binary_and_bare_name(tmp_path):
    (tmp_path / "requirements.txt").write_text("psycopg2-binary==2.8.6\nflask\n")
    fix = runner.find_fix("python", "pg_config executable not found", set())
    subprocess.run(fix["unix"], shell=True, cwd=tmp_path, check=True)
    assert (tmp_path / "requirements.txt").read_text() == "psycopg2-binary\nflask\n"
    (tmp_path / "requirements.txt").write_text("psycopg2\npsycopg2>=2.8\n")
    subprocess.run(fix["unix"], shell=True, cwd=tmp_path, check=True)
    assert (tmp_path / "requirements.txt").read_text() == "psycopg2-binary\npsycopg2-binary\n"


class PartialTestsSandbox:
    def __init__(self, image, name):
        self.image = image

    def start(self): pass
    def destroy(self): pass

    def exec(self, script, timeout):
        if "pytest -q" in script and "pip install -q pytest" in script:
            return 1, "FAILED tests/test_a.py::test_x\n1 failed, 570 passed in 90.5s"
        if "pip install -r" in script:
            return 0, "ok"
        if "package.json" in script:
            return 1, ""
        return 0, "ok"


def test_some_failing_tests_do_not_make_a_working_setup_failed(monkeypatch):
    monkeypatch.setattr(runner, "Sandbox", PartialTestsSandbox)
    b = {"id": "abcdef123456", "os": "linux", "stack": "python", "files": ["requirements.txt"],
         "repo": {"owner": "o", "name": "n"}}
    runner.run_build(b, lambda build: None)
    assert b["status"] == "partially_verified" and "570 tests passed and 1 failed" in b["message"]
    assert [s["status"] for s in b["steps"]][-1] == "failed"


def test_unsupported_repo_message_lists_the_root(monkeypatch, tmp_path):
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "x.db"))
    monkeypatch.setattr(main, "inspect_repo", lambda o, n: {"owner": o, "name": n, "default_branch": "main", "language": "Python",
                                                              "files": ["Makefile", "umbridge.py"], "dirs": ["requirements"]})
    r = TestClient(main.app).post("/api/builds", json={"repo_url": "https://github.com/a/b"})
    assert r.status_code == 422
    d = r.json()["detail"]
    assert "main language as Python" in d and "Root contains: Makefile, umbridge.py, requirements/" in d


def _stub_repo(monkeypatch):
    monkeypatch.setattr(main, "inspect_repo", lambda o, n: {"owner": o, "name": n, "default_branch": "main", "language": "Python",
                                                              "files": ["requirements.txt"], "dirs": []})
    monkeypatch.setattr(main, "run_build", lambda b, s: None)  # builds stay "queued"
    main.HITS.clear()


def test_api_keys_and_daily_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("FORKO_API_KEYS", "alice:secret1,bob:secret2")
    monkeypatch.setenv("FORKO_DAILY_LIMIT", "1")
    _stub_repo(monkeypatch)
    c, body = TestClient(main.app), {"repo_url": "https://github.com/a/b"}
    assert c.post("/api/builds", json=body).status_code == 401
    assert c.post("/api/builds", json=body, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/builds", json=body, headers={"X-API-Key": "secret1"}).status_code == 200
    assert c.post("/api/builds", json=body, headers={"X-API-Key": "secret1"}).status_code == 429
    assert c.post("/api/builds", json=body, headers={"X-API-Key": "secret2"}).status_code == 200
    assert c.get("/api/health").status_code == 200  # health needs no key


def test_busy_server_returns_429(monkeypatch, tmp_path):
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("FORKO_API_KEYS", "alice:secret1,bob:secret2")
    monkeypatch.setenv("FORKO_MAX_ACTIVE", "1")
    _stub_repo(monkeypatch)
    c, body = TestClient(main.app), {"repo_url": "https://github.com/a/b"}
    assert c.post("/api/builds", json=body, headers={"X-API-Key": "secret1"}).status_code == 200
    r = c.post("/api/builds", json=body, headers={"X-API-Key": "secret2"})
    assert r.status_code == 429 and "busy" in r.json()["detail"]


def test_no_keys_means_open_for_local_use(monkeypatch, tmp_path):
    monkeypatch.setenv("FORKO_DB", str(tmp_path / "o.db"))
    monkeypatch.delenv("FORKO_API_KEYS", raising=False)
    _stub_repo(monkeypatch)
    assert TestClient(main.app).post("/api/builds", json={"repo_url": "https://github.com/a/b"}).status_code == 200
