#!/usr/bin/env bash
# Erzeugt eine .env aus .env.example mit frischen, sicheren Zufallswerten.
#
#   bash scripts/init_env.sh clips.example.com admin@example.com
#
# Eine bestehende .env wird nie überschrieben.
set -euo pipefail

cd "$(dirname "$0")/.."

DOMAIN="${1:-}"
EMAIL="${2:-}"
if [[ -z "${DOMAIN}" || -z "${EMAIL}" ]]; then
  echo "Aufruf: bash scripts/init_env.sh <domain> <acme-email>" >&2
  exit 1
fi
if [[ -f .env ]]; then
  echo ".env existiert bereits – nichts geändert." >&2
  exit 1
fi

rand() { python3 -c "import secrets;print(secrets.token_urlsafe($1).replace('-','x').replace('_','y'))"; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }
hex() { python3 -c "import secrets;print(secrets.token_hex(32))"; }

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

if grep -q "=CHANGE_ME" .env; then
  echo "Warnung: In .env stehen noch Platzhalter (CHANGE_ME):" >&2
  grep -n "=CHANGE_ME" .env >&2
fi
echo "✓ .env erstellt (Rechte 600). Bitte TOKEN_ENCRYPTION_KEY zusätzlich sicher außerhalb des Servers aufbewahren –"
echo "  ohne diesen Schlüssel sind gespeicherte OAuth-Tokens nach einer Wiederherstellung unbrauchbar."
