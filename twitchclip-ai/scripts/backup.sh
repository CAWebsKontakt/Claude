#!/usr/bin/env bash
# Tägliches Datenbank-Backup (komprimierter pg_dump) mit Aufbewahrungsfrist.
#
#   bash scripts/backup.sh            # Backup nach ./backups
#   BACKUP_KEEP_DAYS=30 bash scripts/backup.sh
#
# Cron (täglich 03:15):  15 3 * * * cd /opt/twitchclip/twitchclip-ai && bash scripts/backup.sh >> backups/backup.log 2>&1
# Videodateien werden bewusst NICHT gesichert (temporär, werden nach STORAGE_RETENTION_DAYS gelöscht).
set -euo pipefail

cd "$(dirname "$0")/.."
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
DIR="${BACKUP_DIR:-./backups}"
mkdir -p "${DIR}"
chmod 700 "${DIR}"

set -a; . ./.env; set +a
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="${DIR}/twitchclip-${STAMP}.sql.gz"

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-twitchclip}" -d "${POSTGRES_DB:-twitchclip}" \
  --no-owner --no-privileges | gzip -9 > "${FILE}.part"
mv "${FILE}.part" "${FILE}"
chmod 600 "${FILE}"

# Plausibilitätsprüfung: Datei muss gültiges gzip und nicht leer sein
gzip -t "${FILE}"
SIZE=$(wc -c < "${FILE}" | tr -d " ")   # portabel (Linux und macOS)
if [[ "${SIZE}" -lt 1024 ]]; then
  echo "$(date "+%Y-%m-%dT%H:%M:%S%z") FEHLER: Backup verdächtig klein (${SIZE} Bytes): ${FILE}" >&2
  exit 1
fi

find "${DIR}" -name 'twitchclip-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete
echo "$(date "+%Y-%m-%dT%H:%M:%S%z") Backup ok: ${FILE} (${SIZE} Bytes)"
