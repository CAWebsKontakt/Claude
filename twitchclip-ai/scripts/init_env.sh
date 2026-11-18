#!/usr/bin/env bash
# Erzeugt eine .env aus .env.example mit frischen, sicheren Zufallswerten.
#
#   bash scripts/init_env.sh clips.example.com admin@example.com
#
# Eine bestehende .env wird nie überschrieben.
set -euo pipefail

cd "$(dirname "$0")/.."

LOCAL=false
if [[ "${1:-}" == "--local" ]]; then
  # Eigener Rechner (z. B. Mac mit Docker Desktop): http://localhost:8000, ohne Domain/Caddy
  LOCAL=true
  DOMAIN="localhost"
  EMAIL="lokal@localhost"
else
  DOMAIN="${1:-}"
  EMAIL="${2:-}"
fi
if [[ -z "${DOMAIN}" || -z "${EMAIL}" ]]; then
  echo "Aufruf: bash scripts/init_env.sh <domain> <acme-email>   (Server)" >&2
  echo "        bash scripts/init_env.sh --local                 (eigener Rechner, ohne Domain)" >&2
  exit 1
fi
if [[ -f .env ]]; then
  echo ".env existiert bereits – nichts geändert." >&2
  exit 1
fi

# openssl ist auf Linux-Servern und macOS vorinstalliert (python3 auf dem Mac nicht immer)
rand() { openssl rand -base64 "$1" | tr -dc 'A-Za-z0-9'; }   # ohne head: kein SIGPIPE bei pipefail
fernet() { openssl rand -base64 32 | tr '+/' '-_'; }
hex() { openssl rand -hex 32; }

SECRET_KEY="$(rand 48)"
TOKEN_KEY="$(fernet)"
PG_PASS="$(rand 24)"
REDIS_PASS="$(rand 24)"
EVENTSUB="$(hex)"

umask 077
sed \
  -e "s|^APP_BASE_URL=.*|APP_BASE_URL=https://${DOMAIN}|" \
  -e "s|^DOMAIN=.*|DOMAIN=${DOMAIN}|" \
  -e "s|^ACME_EMAIL=.*|ACME_EMAIL=${EMAIL}|" \
  -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" \
  -e "s|^TOKEN_ENCRYPTION_KEY=.*|TOKEN_ENCRYPTION_KEY=${TOKEN_KEY}|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${PG_PASS}|" \
  -e "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=${REDIS_PASS}|" \
  -e "s|^TWITCH_EVENTSUB_SECRET=.*|TWITCH_EVENTSUB_SECRET=${EVENTSUB}|" \
  .env.example > .env
chmod 600 .env

if [[ "${LOCAL}" == "true" ]]; then
  {
    echo ""
    echo "# --- Lokaler Betrieb (init_env.sh --local) ---"
    echo "COMPOSE_FILE=docker-compose.yml:docker-compose.local.yml"
  } >> .env
  bash scripts/env_set.sh APP_BASE_URL "http://localhost:8000" >/dev/null
  bash scripts/env_set.sh APP_ENV development >/dev/null
  bash scripts/env_set.sh SESSION_COOKIE_SECURE false >/dev/null
  echo "✓ Lokaler Modus: Dashboard unter http://localhost:8000 (nur auf diesem Rechner erreichbar)."
fi

if grep -q "=CHANGE_ME" .env; then
  echo "Warnung: In .env stehen noch Platzhalter (CHANGE_ME):" >&2
  grep -n "=CHANGE_ME" .env >&2
fi
echo "✓ .env erstellt (Rechte 600). Bitte TOKEN_ENCRYPTION_KEY zusätzlich sicher außerhalb des Servers aufbewahren –"
echo "  ohne diesen Schlüssel sind gespeicherte OAuth-Tokens nach einer Wiederherstellung unbrauchbar."
