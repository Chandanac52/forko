Forko worker

Real build engine for Forko. Checks a public GitHub repo, builds it in a locked-down
Docker container, runs install, build and tests, and returns real results.

Setup (on a Linux server with Docker, not your laptop)
1. Build one Python image per version the worker may use:
   docker build --build-arg PY_VERSION=3.11 -t forko-python:3.11 -f runner/Dockerfile.python runner
   docker build --build-arg PY_VERSION=3.12 -t forko-python:3.12 -f runner/Dockerfile.python runner
   docker build --build-arg PY_VERSION=3.13 -t forko-python:3.13 -f runner/Dockerfile.python runner
   (3.10 is optional. If a project says it needs a newer Python, Forko retries with the lowest built version that fits.)
2. docker build -t forko-node:20 -f runner/Dockerfile.node runner
3. pip install -r requirements.txt
4. export GITHUB_TOKEN=...   (optional, raises the GitHub API rate limit)
   export FORKO_CORS_ORIGINS=https://your-frontend-domain
5. uvicorn app.main:app --host 0.0.0.0 --port 8000
6. Point the front end at it: VITE_FORKO_API_URL=https://your-worker-domain

API
POST /api/builds {repo_url, os}  -> {build_id}
GET  /api/builds/{id}            -> status, steps, fixes, setup_commands, setup_file_url, verified_on, note
GET  /api/builds/{id}/setup-file -> setup.ps1 or setup.sh
GET  /api/badge/{owner}/{name}.svg
Statuses: queued, analyzing, building, testing, verified, partially_verified, failed.
Step statuses: running, passed, failed, skipped.

What is real
Every step result comes from a command run in the container. Fixes are only listed
when the build actually hit the matching error, and are marked worked: true only if
the retry succeeded. Verification happens in a Linux container; Windows and macOS
commands are translated, not executed there (the API says so in "note").

Sandbox size: containers get 2 GB of memory and a 2 GB scratch disk by default (set FORKO_MEMORY and
FORKO_WORK_SIZE to change; the scratch disk lives in RAM, so keep FORKO_MEMORY larger than FORKO_WORK_SIZE).
Projects that pull in PyTorch or CUDA need far more and are reported as a sandbox limit, not as a setup failure.

Known limits
- Python and Node.js only. No system-package fixes yet (containers have no compilers on purpose).
- Containers have outbound network access (needed for pip and npm). Run on an isolated
  machine with no secrets, and add egress filtering and gVisor or Firecracker before opening to the public.
- Rate limit is per IP in memory; use a reverse proxy and a real queue for production.
- Tests cover the logic and API. The Docker execution path needs your first real run.

Server (Stage A, invite-only)
- API keys: set FORKO_API_KEYS="label:key,label2:key2". Clients send the key in the X-API-Key header.
  If it is not set, the API is open (only acceptable on your own computer).
- Limits: FORKO_DAILY_LIMIT (builds per key per day), FORKO_MAX_ACTIVE (builds running or waiting), FORKO_BUILDS_PER_HOUR (per address).
- deploy/setup_server.sh sets up an Ubuntu 24.04 server (Docker, HTTPS through Caddy, service, firewall). See the comments at its top.
- Scripts: .\run_build.ps1 URL -Api https://your.server -ApiKey KEY
