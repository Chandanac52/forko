Forko worker

Real build engine for Forko. Checks a public GitHub repo, builds it in a locked-down
Docker container, runs install, build and tests, and returns real results.

Setup (on a Linux server with Docker, not your laptop)
1. docker build -t forko-python:3.11 -f runner/Dockerfile.python runner
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

Known limits
- Python and Node.js only. No system-package fixes yet (containers have no compilers on purpose).
- Containers have outbound network access (needed for pip and npm). Run on an isolated
  machine with no secrets, and add egress filtering and gVisor or Firecracker before opening to the public.
- Rate limit is per IP in memory; use a reverse proxy and a real queue for production.
- Tests cover the logic and API. The Docker execution path needs your first real run.
