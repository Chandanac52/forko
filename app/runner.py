"""Forko build engine.

Runs a repository's setup inside a locked-down Docker container and records what
really happened. Nothing here is simulated: every step result comes from a command
executed in the container.
"""
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from .setup_gen import setup_commands

log = logging.getLogger("forko")
PY_IMAGE = os.getenv("FORKO_PY_IMAGE", "forko-python:3.11")
NODE_IMAGE = os.getenv("FORKO_NODE_IMAGE", "forko-node:20")
LOG_LIMIT = 20000
TIMEOUTS = {"clone": 180, "install": 900, "build": 900, "test": 900}
MARKERS = {"requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "package.json", "package-lock.json"}
PY_ENV = "cd /work/repo && python -m venv /work/venv && . /work/venv/bin/activate && python -m pip install -q --upgrade pip && "


class Sandbox:
    """One throwaway container: no capabilities, read-only root, memory/CPU/PID limits."""

    def __init__(self, image: str, name: str):
        self.image, self.name = image, name

    def start(self):
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.name,
             "--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "512",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
             "--tmpfs", "/tmp:rw,exec,size=512m", "--tmpfs", "/work:rw,exec,size=2g,uid=1000,gid=1000",
             "--user", "1000:1000", "-e", "HOME=/work", "-e", "PIP_PROGRESS_BAR=off",
             "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1", "-e", "PYTHONUNBUFFERED=1",
             "-e", "NPM_CONFIG_PROGRESS=false", "-e", "NPM_CONFIG_UPDATE_NOTIFIER=false", "-w", "/work",
             self.image, "sleep", "3600"],
            check=True, capture_output=True, timeout=60)

    def exec(self, script: str, timeout: int) -> tuple[int, str]:
        try:
            p = subprocess.run(["docker", "exec", self.name, "bash", "-c", script],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            self.destroy()
            return 124, f"Timed out after {timeout}s. The container was stopped."

    def destroy(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=30)


# ---------------------------------------------------------------- fix engine
def _py_fix(out: str, applied: set) -> dict | None:
    if re.search(r"pg_config executable not found|libpq-fe\.h", out) and "psycopg2-binary" not in applied:
        unix = r"sed -i.bak -E 's/^psycopg2([=<>~!]|[[:space:]]|$)/psycopg2-binary\1/' requirements.txt"
        return {"id": "psycopg2-binary",
                "what_broke": "Dependency install failed: psycopg2 could not compile.",
                "why": "Building psycopg2 from source needs the PostgreSQL client headers (libpq) and a C compiler, which a clean machine does not have.",
                "what_changed": "Replaced psycopg2 with the prebuilt psycopg2-binary package in requirements.txt.",
                "pre": "cd /work/repo && " + unix, "unix": unix,
                "windows": r"(Get-Content requirements.txt) -replace '^psycopg2(?=[=<>~!\s]|$)','psycopg2-binary' | Set-Content requirements.txt"}
    m = re.search(r"(?:satisfies the requirement|No matching distribution found for) ([A-Za-z0-9_.\-]+)==", out)
    if m and f"unpin-{m.group(1).lower()}" not in applied:
        pkg = m.group(1)
        pat = pkg.replace(".", r"\.")
        unix = f"sed -i.bak -E 's/^{pat}==[^ ;#]*/{pkg}/' requirements.txt"
        return {"id": f"unpin-{pkg.lower()}",
                "what_broke": f"Dependency install failed: the pinned version of {pkg} is not available.",
                "why": "The exact version no longer exists, or it does not support this Python version.",
                "what_changed": f"Removed the exact version pin for {pkg} so pip picks a compatible release. Review this before committing it.",
                "pre": "cd /work/repo && " + unix, "unix": unix,
                "windows": f"(Get-Content requirements.txt) -replace '^{pat}==[^\\s;#]*','{pkg}' | Set-Content requirements.txt"}
    return None


def _node_fix(out: str, applied: set) -> dict | None:
    if "ERESOLVE" in out and "legacy-peer-deps" not in applied:
        return {"id": "legacy-peer-deps",
                "what_broke": "npm install failed with a peer dependency conflict (ERESOLVE).",
                "why": "Newer npm versions enforce peer dependency ranges that older projects do not satisfy.",
                "what_changed": "Installed with --legacy-peer-deps, which uses the older, more lenient resolution.",
                "install_override": "npm install --legacy-peer-deps"}
    return None


def find_fix(stack: str, out: str, applied: set) -> dict | None:
    return (_py_fix if stack == "python" else _node_fix)(out, applied)


# ------------------------------------------------------------------ pipeline
def _clip(text: str) -> str:
    if len(text) <= LOG_LIMIT:
        return text
    return text[:2000] + "\n\n... log shortened ...\n\n" + text[-(LOG_LIMIT - 2000):]


def _start(build, save, name):
    s = {"name": name, "status": "running", "duration_s": None, "log": ""}
    build["steps"].append(s)
    save(build)
    return s, time.monotonic()


def _finish(build, save, s, t0, status, text):
    s.update(status=status, duration_s=round(time.monotonic() - t0, 1), log=_clip(text))
    save(build)
    return status


def _install_cmds(stack, files):
    if stack == "python":
        cmds = []
        if "requirements.txt" in files:
            cmds.append("pip install -r requirements.txt")
        if "requirements-dev.txt" in files:
            cmds.append("pip install -r requirements-dev.txt")
        return cmds or ["pip install -e ."]
    return ["npm ci" if "package-lock.json" in files else "npm install"]


def _install(sb, stack, files):
    """Install dependencies, applying known fixes (max 3 retries). Returns (ok, log, final_cmds, fixes)."""
    cmds, fixes, text = _install_cmds(stack, files), [], ""
    for _ in range(4):
        script = (PY_ENV + " && ".join(cmds)) if stack == "python" else "cd /work/repo && " + cmds[0]
        code, out = sb.exec(script, TIMEOUTS["install"])
        text += out
        if code == 0:
            for f in fixes:
                f["worked"] = True
            return True, text, cmds, fixes
        fix = find_fix(stack, out, {f["id"] for f in fixes})
        if not fix:
            break
        if fix.get("pre"):
            sb.exec(fix["pre"], 60)
        if fix.get("install_override"):
            cmds = [fix["install_override"]]
        text += f"\n--- Forko applied a fix: {fix['what_changed']} Retrying. ---\n"
        fixes.append({k: v for k, v in fix.items() if k in ("id", "what_broke", "why", "what_changed", "unix", "windows")} | {"worked": False})
    return False, text, cmds, fixes


def _package_scripts(sb) -> dict:
    import json
    code, out = sb.exec("cat /work/repo/package.json", 30)
    try:
        return json.loads(out).get("scripts", {}) if code == 0 else {}
    except ValueError:
        return {}


def _fail(build, step):
    build["status"] = "failed"
    build["message"] = f"Failed at: {step}. See the step log for the real error."


def _run(build, save):
    o, n = build["repo"]["owner"], build["repo"]["name"]
    stack, files = build["stack"], set(build["files"])
    build["status"] = "analyzing"
    s, t = _start(build, save, "Detect stack")
    _finish(build, save, s, t, "passed", f"Detected a {stack} project from: {', '.join(sorted(files & MARKERS))}")

    sb = Sandbox(PY_IMAGE if stack == "python" else NODE_IMAGE, f"forko-{build['id'][:12]}")
    try:
        build["status"] = "building"
        sb.start()

        s, t = _start(build, save, "Clone repository")
        code, out = sb.exec(f"GIT_TERMINAL_PROMPT=0 git clone --depth 1 https://github.com/{o}/{n}.git /work/repo 2>&1 "
                            f"&& git -C /work/repo log -1 --format='Commit %h %s'", TIMEOUTS["clone"])
        if _finish(build, save, s, t, "passed" if code == 0 else "failed", out) == "failed":
            return _fail(build, "Clone repository")

        s, t = _start(build, save, "Install dependencies")
        ok, out, cmds, fixes = _install(sb, stack, files)
        build["fixes"], build["install_final"] = fixes, cmds
        if _finish(build, save, s, t, "passed" if ok else "failed", out) == "failed":
            return _fail(build, "Install dependencies")

        s, t = _start(build, save, "Build")
        if stack == "python":
            code, out = sb.exec("cd /work/repo && . /work/venv/bin/activate && python -m compileall -q -x '/(\\.git|\\.venv|venv|node_modules)/' .", TIMEOUTS["build"])
            out = out or "Compile check passed (this checks syntax; Python projects usually have no build step)."
            st, build["build_ran"] = ("passed" if code == 0 else "failed"), True
        elif "build" in (scripts := _package_scripts(sb)):
            code, out = sb.exec("cd /work/repo && npm run build", TIMEOUTS["build"])
            st, build["build_ran"] = ("passed" if code == 0 else "failed"), True
        else:
            st, out = "skipped", "No build script in package.json."
        if _finish(build, save, s, t, st, out) == "failed":
            return _fail(build, "Build")

        build["status"] = "testing"
        s, t = _start(build, save, "Run tests")
        if stack == "python":
            code, out = sb.exec("cd /work/repo && . /work/venv/bin/activate && pip install -q pytest && python -m pytest -q -x -p no:cacheprovider", TIMEOUTS["test"])
            st = "skipped" if code == 5 else "passed" if code == 0 else "failed"
            out = out + "\nNo tests were found." if code == 5 else out
        else:
            test = _package_scripts(sb).get("test", "")
            if test and "no test specified" not in test:
                code, out = sb.exec("cd /work/repo && CI=true npm test", TIMEOUTS["test"])
                st = "passed" if code == 0 else "failed"
            else:
                st, out = "skipped", "No test script in package.json."
        build["tests_ran"] = st == "passed"
        if _finish(build, save, s, t, st, out) == "failed":
            return _fail(build, "Run tests")

        if st == "skipped":
            build["status"] = "partially_verified"
            build["message"] = "Dependencies installed and the project builds, but no automated tests were found, so Forko could not confirm it runs correctly."
        else:
            build["status"] = "verified"
            build["message"] = "Dependencies installed, the project builds, and its tests passed in a clean Linux container."
    finally:
        sb.destroy()


def run_build(build: dict, save) -> None:
    build.update(build_ran=False, tests_ran=False, install_final=[], fixes=[], steps=[])
    build["runtime"] = ("Python " + PY_IMAGE.split(":")[-1]) if build["stack"] == "python" else ("Node " + NODE_IMAGE.split(":")[-1])
    try:
        _run(build, save)
    except FileNotFoundError:
        build["status"], build["message"] = "failed", "Docker is not available on the Forko worker."
    except subprocess.CalledProcessError as e:
        log.error("docker error: %s", e.stderr)
        build["status"], build["message"] = "failed", "The build container could not be started."
    except Exception:
        log.exception("build crashed")
        build["status"], build["message"] = "failed", "Internal error while running the build."
    if build["status"] in ("verified", "partially_verified"):
        build["verified_on"] = f"Linux container ({build['runtime']}, no system build tools)"
        build["setup_commands"] = setup_commands(build)
        build["setup_file_url"] = f"/api/builds/{build['id']}/setup-file"
        if build["os"] != "linux":
            build["note"] = (f"Verified in a Linux container. Commands for {build['os']} are translated "
                             f"from the verified steps and were not executed on {build['os']}.")
    build["finished_at"] = datetime.now(timezone.utc).isoformat()
    save(build)