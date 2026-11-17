#!/usr/bin/env bash
# Stellt ein Backup wieder her. ACHTUNG: überschreibt die aktuelle Datenbank!
#
#   bash scripts/restore.sh backups/twitchclip-20261117-031500.sql.gz
set -euo pipefail

cd "$(dirname "$0")/.."
FILE="${1:-}"
if [[ -z "${FILE}" || ! -f "${FILE}" ]]; then
  echo "Aufruf: bash scripts/restore.sh <backup.sql.gz>" >&2
  exit 1
fi
set -a; . ./.env; set +a

read -r -p "Datenbank '${POSTGRES_DB:-twitchclip}' wirklich mit ${FILE} überschreiben? (ja/nein) " answer
[[ "${answer}" == "ja" ]] || { echo "Abgebrochen."; exit 1; }

docker compose stop web worker worker-media scheduler
docker compose exec -T postgres psql -U "${POSTGRES_USER:-twitchclip}" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"${POSTGRES_DB:-twitchclip}\" WITH (FORCE);" \
  -c "CREATE DATABASE \"${POSTGRES_DB:-twitchclip}\" OWNER \"${POSTGRES_USER:-twitchclip}\";"
gunzip -c "${FILE}" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-twitchclip}" -d "${POSTGRES_DB:-twitchclip}" -v ON_ERROR_STOP=1 -q
docker compose up -d
echo "✓ Wiederherstellung abgeschlossen. Hinweis: OAuth-Tokens sind nur mit dem ursprünglichen TOKEN_ENCRYPTION_KEY lesbar."
