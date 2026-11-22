#!/usr/bin/env bash
# Forko server setup for Ubuntu 24.04 (run as root, once).
#   1. Upload forko-worker.zip to /tmp, then:
#        apt-get update && apt-get install -y unzip
#        unzip -o /tmp/forko-worker.zip -d /opt
#        bash /opt/forko-worker/deploy/setup_server.sh YOUR.DOMAIN.NAME
# Safe to re-run. It prints the first API key once at the end (only when it creates the settings file).
set -euo pipefail
DOMAIN="${1:?usage: bash setup_server.sh your.domain.name}"
APP=/opt/forko-worker
export DEBIAN_FRONTEND=noninteractive

echo "== 1/8 system packages"
apt-get update
apt-get install -y ca-certificates curl gnupg unzip python3-venv python3-pip ufw iptables-persistent \
  debian-keyring debian-archive-keyring apt-transport-https

echo "== 2/8 Docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

echo "== 3/8 Caddy (HTTPS reverse proxy)"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

echo "== 4/8 service user and folders"
id forko >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin forko
usermod -aG docker forko
mkdir -p /var/lib/forko
chown -R forko:forko /var/lib/forko "$APP"

echo "== 5/8 Python environment"
sudo -u forko python3 -m venv "$APP/.venv"
sudo -u forko "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "== 6/8 clean build environments (this takes a few minutes)"
cd "$APP"
for v in 3.11 3.12 3.13; do
  docker build --build-arg PY_VERSION=$v -t forko-python:$v -f runner/Dockerfile.python runner
done
docker build -t forko-node:20 -f runner/Dockerfile.node runner

echo "== 7/8 settings, service, web server"
NEWKEY=""
if [ ! -f /etc/forko.env ]; then
  NEWKEY="$(openssl rand -hex 24)"
  cat > /etc/forko.env <<ENV
FORKO_API_KEYS=admin:${NEWKEY}
FORKO_DB=/var/lib/forko/forko.db
# Put the address of your front end here (for example https://your-site.example.com). "*" is for first tests only.
FORKO_CORS_ORIGINS=*
# A GitHub token with no permissions (public repos only) raises GitHub's rate limit. Do not give it any scopes.
GITHUB_TOKEN=
FORKO_WORKERS=1
FORKO_MAX_ACTIVE=4
FORKO_DAILY_LIMIT=20
FORKO_BUILDS_PER_HOUR=30
FORKO_DISABLE_DOCS=1
ENV
  chmod 600 /etc/forko.env
fi
cat > /etc/systemd/system/forko.service <<UNIT
[Unit]
Description=Forko worker
After=network-online.target docker.service
Wants=network-online.target
Requires=docker.service

[Service]
User=forko
Group=forko
WorkingDirectory=$APP
EnvironmentFile=/etc/forko.env
ExecStart=$APP/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    reverse_proxy 127.0.0.1:8000
}
CADDY
systemctl daemon-reload
systemctl enable forko
systemctl restart forko
systemctl restart caddy

echo "== 8/8 firewall and safety rules"
ufw default deny incoming
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
# Build containers must not reach the cloud provider's internal metadata service.
iptables -C DOCKER-USER -d 169.254.169.254 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
netfilter-persistent save
echo '0 4 * * 0 root docker system prune -f >/dev/null 2>&1' > /etc/cron.d/forko-prune

echo
echo "Done. Check:  curl https://$DOMAIN/api/health"
if [ -n "$NEWKEY" ]; then
  echo "Your first API key (shown once, save it now): $NEWKEY"
else
  echo "Settings file already existed, so no new API key was created. Keys live in /etc/forko.env"
fi
