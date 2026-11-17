#!/usr/bin/env bash
# Aktualisiert die Anwendung: Backup, neuen Code holen, neu bauen, neu starten.
#
#   bash scripts/update.sh
set -euo pipefail

cd "$(dirname "$0")/.."
echo "==> Backup vor dem Update"
bash scripts/backup.sh
echo "==> Code aktualisieren"
git pull --ff-only
echo "==> Image bauen und Dienste neu starten (Migrationen laufen automatisch)"
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
echo "✓ Update abgeschlossen"
