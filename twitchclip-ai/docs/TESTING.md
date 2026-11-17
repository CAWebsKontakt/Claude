# Tests

## Automatisierte Tests

Die Suite (100 Tests) läuft ohne externe Dienste: SQLite im Speicher und `fakeredis`. Externe APIs
(Twitch/TikTok, ab Phase 2) werden mit `respx` gemockt – es gibt keine echten Netzwerkaufrufe.

```bash
# lokal (Python 3.12)
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
ruff check . && ruff format --check .

# dieselben Tests gegen echtes PostgreSQL (inkl. Abgleich Migration ↔ Modelle)
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/twitchclip_test python -m pytest -q

# im Docker-Image (identisch zur Produktion, läuft als Nicht-Root-Benutzer)
docker compose --profile test run --rm --build test
```

| Datei | Prüft |
|---|---|
| `test_config.py` | `STREAMER_LIST`-Parsing, Grenzwerte, Zeitzone, Pflicht-Secrets in Produktion, keine Secrets in der Anzeige |
| `test_security.py` | Fernet-Verschlüsselung inkl. Schlüsselrotation, Tokens verschlüsselt in der DB, Argon2id, Passwortregeln, Login-Rate-Limit |
| `test_auth.py` | Login/Logout, sichere Cookies, CSRF, Brute-Force-Sperre, Open-Redirect, Sitzungswiderruf, Sicherheits-Header |
| `test_users.py` | Benutzerverwaltung, Rollen, Schutz des letzten Admins |
| `test_streamers.py` | Hinzufügen (Name/URL), Duplikate, Rollen, kompletter Rechte-Ablauf Modus A → B, Ablauf/Widerruf, Löschen |
| `test_storage.py` | Aufbewahrungsfrist, Probelauf, geschützte Videos in Freigabe, harte Speichergrenze, Path-Traversal |
| `test_workers.py` | keine doppelten Jobs, Scheduler-Locks, echter Job-Durchlauf im RQ-Worker, Job-ID-Validierung |
| `test_health_system.py` | `/healthz`, `/readyz` inkl. Redis-Ausfall, Systemseite, manuelle Bereinigung ohne Doppelung |
| `test_pages.py` | alle Dashboard-Seiten mit Beispieldaten, mobile Meta-Tags, Aktionen, Rechte der Betrachter-Rolle |
| `test_media.py` | Videoauslieferung nur mit Login, HTTP-Range (Spulen), keine Dateien außerhalb von `/data` |
| `test_migrations.py` | Upgrade/Downgrade auf SQLite; auf PostgreSQL zusätzlich: Migration entspricht exakt den Modellen |
| `test_cli.py` | Secrets erzeugen, Benutzer/Passwort, Streamer per CLI, Konfigurationsprüfung, Token-Key-Rotation |

## Phase 1 – Abnahme auf dem VPS

Nach `docker compose up -d --build` (siehe INSTALL.md):

```bash
cd /opt/twitchclip/twitchclip-ai

# 1. Alle Dienste laufen?
docker compose ps
#   erwartet: postgres/redis/web healthy, migrate exited (0), worker/worker-media/scheduler/caddy running

# 2. Gesundheit lokal und über HTTPS
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/readyz            # {"status":"ok","database":"ok","redis":"ok",...}
curl -fsS https://clips.ihre-domain.de/healthz     # gültiges Let's-Encrypt-Zertifikat
curl -sI http://clips.ihre-domain.de | head -1     # 308 Weiterleitung auf HTTPS

# 3. Migration und Import
docker compose logs migrate | tail -5               # "Bootstrap abgeschlossen"
docker compose exec web python -m app.cli check-config
docker compose exec web python -m app.cli list-streamers

# 4. Admin anlegen und anmelden
docker compose exec web python -m app.cli create-user --username admin --role admin
#   Browser: https://clips.ihre-domain.de → Login

# 5. Streamer-Ablauf im Dashboard
#   Streamer hinzufügen → Detailseite → "Neue Erlaubnis dokumentieren" → Status "Twitch-Autorisierung fehlt"
#   Erlaubnis widerrufen → Streamer fällt automatisch auf Modus A zurück (Aktivitätsprotokoll prüfen)

# 6. Hintergrundjobs laufen (Scheduler → Worker)
docker compose logs scheduler | grep "Periodische Jobs eingereiht"
docker compose logs worker | grep "Job OK"

# 7. Tests im Produktionsimage
docker compose --profile test run --rm --build test

# 8. Backup + Healthcheck-Skript
bash scripts/backup.sh && ls -lh backups/
bash scripts/healthcheck.sh; echo "Exit-Code: $?"   # 0 = alles ok

# 9. Selbstheilung: Worker-Prozess hart beenden → Docker startet ihn neu
sudo kill -9 "$(docker inspect twitchclip-worker-1 --format '{{.State.Pid}}')"
sleep 10 && docker compose ps worker

# 10. Reboot-Test
sudo reboot
#   danach: docker compose ps && curl -fsS http://127.0.0.1:8000/readyz

# 11. Sicherheit
curl -sI https://clips.ihre-domain.de/login | grep -iE "strict-transport|content-security|x-frame"
nc -zv IHRE-SERVER-IP 5432 ; nc -zv IHRE-SERVER-IP 6379 ; nc -zv IHRE-SERVER-IP 8000   # von außen: alle geschlossen
```

## Lokale Entwicklung ohne Docker

```bash
# PostgreSQL und Redis lokal (oder per Docker) bereitstellen, dann:
export APP_ENV=development SESSION_COOKIE_SECURE=false APP_BASE_URL=http://localhost:8000 \
       DATABASE_URL=postgresql+psycopg://twitchclip:twitchclip@localhost:5432/twitchclip \
       REDIS_URL=redis://localhost:6379/0 DATA_DIR=./data \
       TOKEN_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
python -m app.cli bootstrap
python -m app.cli create-user --username admin --role admin
uvicorn app.main:create_app --factory --reload &
python -m app.workers.worker default publish media &
python -m app.workers.scheduler
```
