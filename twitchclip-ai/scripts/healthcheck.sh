#!/usr/bin/env bash
# Externe Betriebsüberwachung per Cron. Prüft Container, /readyz, freien Speicher und
# fehlgeschlagene Jobs. Optional Alarm an einen Webhook (ntfy, Discord, Slack, Telegram-Bot …).
#
#   */5 * * * * cd /opt/twitchclip/twitchclip-ai && bash scripts/healthcheck.sh >> backups/health.log 2>&1
# (ALERT_WEBHOOK_URL aus der Umgebung oder der .env). Ergänzt die Alarme der Anwendung: meldet auch, wenn
# Container, Scheduler oder Redis selbst ausgefallen sind.
set -uo pipefail

cd "$(dirname "$0")/.."
PROBLEMS=()
# Ohne Umgebungsvariable den Webhook aus der .env verwenden (gleiches Ziel wie die Alarme der Anwendung)
if [[ -z "${ALERT_WEBHOOK_URL:-}" && -f .env ]]; then
  ALERT_WEBHOOK_URL="$(grep -E '^ALERT_WEBHOOK_URL=' .env | tail -1 | cut -d= -f2- | tr -d '"')"
fi

for svc in web worker worker-media scheduler postgres redis caddy; do
  state="$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk -v s="$svc" '$1==s {print $2}')"
  [[ "${state}" == "running" ]] || PROBLEMS+=("Dienst ${svc}: ${state:-nicht vorhanden}")
done

ready="$(curl -fsS --max-time 10 http://127.0.0.1:8000/readyz 2>/dev/null || true)"
echo "${ready}" | grep -q '"status":"ok"' || PROBLEMS+=("readyz meldet: ${ready:-keine Antwort}")

free_gb=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
[[ "${free_gb:-0}" -ge 5 ]] || PROBLEMS+=("Nur noch ${free_gb} GB frei auf /")

failed="$(docker compose exec -T worker python -c "
from rq.registry import FailedJobRegistry
from app.redis_client import get_redis
from app.workers.queues import ALL_QUEUES, get_queue
print(sum(FailedJobRegistry(queue=get_queue(q, get_redis())).count for q in ALL_QUEUES))
" 2>/dev/null | tail -1)"
[[ "${failed:-0}" =~ ^[0-9]+$ && "${failed:-0}" -le 20 ]] || PROBLEMS+=("Fehlgeschlagene Jobs: ${failed:-unbekannt}")

if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
  echo "$(date -Is) OK"
  exit 0
fi

MESSAGE="TwitchClip AI ($(hostname)): $(IFS='; '; echo "${PROBLEMS[*]}")"
echo "$(date -Is) ${MESSAGE}"
if [[ -n "${ALERT_WEBHOOK_URL:-}" ]]; then
  curl -fsS --max-time 10 -H "Content-Type: text/plain; charset=utf-8" -d "${MESSAGE}" "${ALERT_WEBHOOK_URL}" >/dev/null || true
fi
exit 1
