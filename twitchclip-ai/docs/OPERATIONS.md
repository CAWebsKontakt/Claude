# Betrieb: Streamer verwalten, überwachen, warten

Alle Befehle im Projektverzeichnis auf dem Server (`/opt/twitchclip/twitchclip-ai`) als Benutzer `twitchclip`.

## 1. Streamer hinzufügen

**A) Dashboard (empfohlen)** – *Streamer → Streamer hinzufügen*: Twitch-Name, `@name` oder Kanal-URL
(`https://twitch.tv/name`) eingeben. Neue Kanäle starten immer in **Modus A** (nur öffentliche Metadaten).

**B) Kommandozeile**

```bash
docker compose exec web python -m app.cli add-streamer kanal1 https://twitch.tv/kanal2
docker compose exec web python -m app.cli list-streamers
```

**C) `STREAMER_LIST` in der `.env`** – kommagetrennt. Wird bei jedem `docker compose up -d` importiert; vorhandene
Kanäle bleiben unverändert, entfernt wird nie etwas automatisch. Danach ist das Dashboard maßgeblich.

### Streamer auf Modus B umstellen

1. Schriftliche Erlaubnis des Streamers oder eines berechtigten Editors einholen (E-Mail, Vertrag, Formular) –
   mit Umfang: nur Verarbeitung oder auch TikTok-Veröffentlichung, ggf. Einschränkungen.
2. Dashboard → Streamer → *Neue Erlaubnis dokumentieren*: Wer, Rolle, Nachweis (Ablageort), Gültigkeit,
   Umfang, Bestätigung. Option „Danach auf Modus B umstellen“.
3. Ab Phase 3: Autorisierungslink an den Streamer/Editor senden (Twitch-OAuth).
4. Der Status wechselt dann zu „Nur Verarbeitung erlaubt“ bzw. „Verarbeitung & Veröffentlichung erlaubt“.

Widerruf: in der Erlaubnis-Tabelle *Widerrufen* (mit Grund). Ist keine gültige Erlaubnis mehr vorhanden, fällt
der Streamer **automatisch** auf Modus A zurück. Abgelaufene Erlaubnisse (Gültig bis) wirken genauso.

### Streamer entfernen

Detailseite → *Streamer entfernen* → Twitch-Namen zur Bestätigung eintippen. Löscht Nachweise, Tokens, Clips und
Highlights; das Aktivitätsprotokoll bleibt erhalten. Alternativ *Überwachung pausieren*, um Daten zu behalten.

## 2. Benutzer

* Dashboard → *Benutzer* (nur Admins): anlegen, Rolle ändern, deaktivieren, Passwort setzen.
* Rollen: **Administrator** (alles), **Redakteur** (Streamer, Rechte, Highlights, Freigaben), **Betrachter** (lesen).
* Passwort vergessen:

  ```bash
  docker compose exec web python -m app.cli reset-password --username admin
  ```

## 3. Überwachung

| Was | Wie |
|---|---|
| Containerstatus | `docker compose ps` |
| Logs (JSON) | `docker compose logs -f --tail=100 web worker worker-media scheduler` |
| Nur Fehler | `docker compose logs --since 1h \| grep '"level": "ERROR"'` |
| Bereitschaft | `curl -fsS http://127.0.0.1:8000/readyz` → `{"status":"ok",...}`; extern `https://<domain>/readyz` |
| Dashboard | *System*: DB, Redis, Worker, Scheduler-Herzschlag, Queue-Längen, fehlgeschlagene Jobs, Speicher, Konfiguration |
| Fehlerprotokoll | *Aktivität & Fehler* mit Filter `Stufe = error`; Übersicht zeigt „Fehler (24 h)“ |
| Ressourcen | `docker stats --no-stream` |
| Speicher | `df -h /` und `docker system df` |
| Alarm | `scripts/healthcheck.sh` per Cron (Abschnitt 4 in INSTALL.md), Alarm z. B. per <https://ntfy.sh> aufs Handy |

Externe Uptime-Überwachung (z. B. UptimeRobot, Healthchecks.io, Uptime Kuma) auf
`https://<domain>/readyz` einrichten – der Endpunkt liefert HTTP 503, wenn Datenbank oder Redis ausfallen, und
gibt keine sensiblen Daten preis.

Fehlgeschlagene Jobs ansehen und erneut ausführen:

```bash
docker compose exec worker sh -c 'rq info --url "$REDIS_URL"'
docker compose exec worker python - <<'PY'
from rq.registry import FailedJobRegistry
from app.redis_client import get_redis
from app.workers.queues import ALL_QUEUES, get_queue
for name in ALL_QUEUES:
    reg = FailedJobRegistry(queue=get_queue(name, get_redis()))
    for job_id in reg.get_job_ids():
        print(name, job_id)
        # reg.requeue(job_id)   # zum erneuten Ausführen einkommentieren
PY
```

## 4. Speicher

* Automatisch: stündliche Bereinigung (`STORAGE_RETENTION_DAYS`, `STORAGE_MAX_GB`), Videos in offener Freigabe
  bleiben geschützt. Docker-Logs sind auf 5 × 10 MB je Container begrenzt.
* Manuell:

  ```bash
  docker compose exec worker python -m app.cli cleanup-storage --dry-run   # anzeigen
  docker compose exec worker python -m app.cli cleanup-storage             # löschen
  docker image prune -f                                                    # alte Images
  ```

## 5. Sicherung und Wiederherstellung

```bash
bash scripts/backup.sh                                     # sofortiges Backup nach ./backups
bash scripts/restore.sh backups/twitchclip-YYYYMMDD-HHMMSS.sql.gz
```

Zu sichern sind: `backups/*.sql.gz` **und** die `.env` (enthält `TOKEN_ENCRYPTION_KEY`) – getrennt voneinander.

## 6. Schlüssel rotieren

* `SECRET_KEY` ändern → alle Benutzer werden abgemeldet. `docker compose up -d`.
* `TOKEN_ENCRYPTION_KEY` rotieren:

  ```bash
  NEW=$(docker compose exec -T web python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" | tr -d '\r')
  # in .env: TOKEN_ENCRYPTION_KEY=<NEU>,<ALT>
  docker compose up -d
  docker compose exec web python -m app.cli rotate-token-key
  # danach in .env nur noch TOKEN_ENCRYPTION_KEY=<NEU>, erneut docker compose up -d
  ```

## 7. Fehlerbehebung

| Symptom | Ursache / Lösung |
|---|---|
| `migrate` exited (1) | `docker compose logs migrate` – meist falsches `POSTGRES_PASSWORD` nach Änderung (Passwort ist beim ersten Start in das Volume geschrieben) |
| `web` startet nicht, „SECRET_KEY fehlt“ | `.env` prüfen: `docker compose run --rm --no-deps web python -m app.cli check-config` |
| Kein HTTPS-Zertifikat | DNS zeigt nicht auf den Server oder Port 80/443 blockiert: `docker compose logs caddy` |
| Login „Zu viele Fehlversuche“ | 15 Minuten warten oder `docker compose exec redis sh -c "redis-cli --scan --pattern 'tcai:login-fail:*' \| xargs -r redis-cli del"` |
| System-Seite: „Kein RQ-Worker aktiv“ | `docker compose up -d worker worker-media`, Logs prüfen |
| System-Seite: „Scheduler sendet keinen Herzschlag“ | `docker compose restart scheduler` |
