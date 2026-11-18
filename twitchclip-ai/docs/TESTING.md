# Tests

## Automatisierte Tests

Die Suite (265 Tests) läuft ohne externe Dienste: SQLite im Speicher und `fakeredis`. Externe APIs
(Twitch, TikTok) werden mit `respx` gemockt – es gibt keine echten Netzwerkaufrufe.

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
| `test_twitch_client.py` | App-Token (Cache, verschlüsselt, 401-Erneuerung), Rate-Limit/429, 5xx-Wiederholung, Batching, Paginierung, EventSub-API |
| `test_eventsub.py` | HMAC-Signatur, Alter, Duplikate, Challenge, online/offline per Webhook inkl. Folgejobs, Widerruf |
| `test_twitch_monitor.py` | IDs auflösen, Umbenennung, Live-Lebenszyklus, Karenzzeit, VOD-Zuordnung, EventSub-Abgleich, Kostenlimit |
| `test_clip_discovery.py` | Zeitfenster, Bewertung (relativ, Häufung, Länge), Upsert ohne Duplikate, Empfehlungen, Aufbewahrung |
| `test_twitch_jobs.py` | Jobs ohne Konfiguration/Testmodus, Scheduler, Folgejobs, gedrosselte Fehler, Rate-Limit-Abbruch, Dashboard |
| `test_twitch_authorization.py` | Einladungslink (Signatur, Ablauf, einmalig), kompletter OAuth-Ablauf über HTTP, Editor-Rolle, fehlende Scopes, Token-Erneuerung/-Sperre, stündliche Validierung, EventSub-Widerruf, Trennen im Dashboard |
| `test_clip_processing.py` | Zeitangaben, Kandidatenauswahl ohne Überlappung, Rechteprüfung vor jedem Schritt, Clip aus VOD (Parameter), Download ohne Token zum CDN, SHA-256-Duplikate, Größenlimit, Wiederholungen, 403 bei Editor, Testmodus mit echten FFmpeg-Beispielvideos, Neustart nach Re-Autorisierung |
| `test_analysis.py` | Ton-Ausbrüche, Sprachsignale, Fensterwahl, Bewertung mit Umverteilung, Bild-Hashes, Facecam-Erkennung, Chat-Zähler ohne Texte, Chat-Spitzen als Kandidaten, Chat-Abo, Auswahl der besten N mit Schwelle und Tageslimit, Analyse echter FFmpeg-Testvideos, Duplikat gleicher VOD-Moment |
| `test_tiktok.py` | Stückregeln der Content Posting API, PKCE (S256), kompletter OAuth-Ablauf über HTTP inkl. verschlüsselter Tokens, falscher/abgelaufener State, Rollen, Fehlerzuordnung (401/429/5xx/Fehlercodes), `Content-Range` je Stück, Token-Erneuerung und -Sperre, Trennen mit Widerruf, Creator-Info |
| `test_publishing.py` | Warteschlange nur mit Rechten und fertigem Video, alle Freigabe-Pflichtangaben und UX-Regeln, Direct Post nur mit Schalter + Scope, Testmodus nur Export, Videolänge, ZIP-Export, Direct Post und Inbox Ende-zu-Ende, Fehlererklärung + erneute Prüfung, Rechteentzug vor Upload, nie doppelt initialisieren, Rate-Limit, Wiederaufnahme verlorener Jobs, Dashboard ohne Voreinstellungen |
| `test_operations.py` | Alarme (System + fachlich: Twitch-OAuth, auslaufende Erlaubnis, TikTok-Konto, fehlgeschlagene Uploads, Freigabestau), einmalige Meldung + Entwarnung, Discord/Slack/Klartext, Zustellfehler, `/metrics` nur mit Token, eindeutige Selbsttest-Videos, kompletter Selbsttest mit echtem FFmpeg, CLI |
| `test_rendering.py` | Layout-Wahl und Ausschnitte innerhalb der Quelle, Untertitel-Zeilen/Karaoke/Überschrift, SRT, Titel/Beschreibung/max. 5 Hashtags, echtes Rendering beider Layouts (1080×1920, H.264/AAC, 30 fps), Aufräumen alter Fassungen, Schutz von Fassungen in der Warteschlange, Rechteprüfung, Dashboard-Bearbeitung, automatischer Schnitt nach der Auswahl |

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

## Phase 2 – Abnahme auf dem VPS (Twitch-Überwachung)

Voraussetzung: `TWITCH_CLIENT_ID` und `TWITCH_CLIENT_SECRET` in der `.env`, Domain mit HTTPS erreichbar.

```bash
cd /opt/twitchclip/twitchclip-ai
git pull   # bzw. Patch einspielen: git am /pfad/TwitchClip-Phase-2.patch
docker compose up -d --build
docker compose logs migrate | grep "0002"                 # Migration Twitch-Überwachung gelaufen

# 1. Tests im Image (inkl. gemockter Twitch-API)
docker compose --profile test run --rm --build test       # alle Tests bestanden

# 2. Konfiguration: EventSub aktiv?
docker compose exec web python -m app.cli check-config | grep -E "TWITCH_EVENTSUB|TWITCH_CLIENT"

# 3. Streamer anlegen (öffentlicher Kanal, z. B. ein großer deutscher Streamer)
docker compose exec web python -m app.cli add-streamer <kanalname>
sleep 60
docker compose logs worker | grep -E "twitch|Job OK" | tail -20

# 4. Twitch-ID, Abos, Clips prüfen
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select twitch_login, twitch_user_id, is_live, last_clip_scan_at from streamers;"
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select type, status from eventsub_subscriptions;"          # erwartet: 2 × enabled je Streamer
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select title, view_count, round(discovery_score::numeric,1), status from twitch_clips order by discovery_score desc limit 10;"

# 5. Webhook von außen erreichbar? (ohne Signatur muss 403 kommen – nicht 404/502)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://clips.ihre-domain.de/webhooks/twitch/eventsub   # 403

# 6. Live-Erkennung: Wenn der Kanal live geht, erscheint innerhalb von Sekunden
#    im Aktivitätsprotokoll "… ist live (EventSub)", beim Ende "… ist offline (EventSub)".
docker compose logs -f web | grep -i eventsub
```

Im Dashboard prüfen: *Streamer* (Profilbild, Live-Status), *Öffentliche Clips* (Bewertung, „Warum diese
Bewertung?“, Abspielen im Twitch-Player), *System → Twitch* (Abos aktiv, Kosten, API-Kontingent).

## Phase 3 – Abnahme auf dem VPS (Autorisierung & Clip-Verarbeitung)

Voraussetzung: Phase 2 läuft, ein Streamer hat zugestimmt, an dieser Abnahme teilzunehmen (oder Sie testen mit
Ihrem **eigenen** Twitch-Kanal – dann sind Sie selbst der Kanalinhaber).

```bash
cd /opt/twitchclip/twitchclip-ai
git am /pfad/TwitchClip-Phase-3.patch && docker compose up -d --build
docker compose logs migrate | grep 0003                     # Migration Autorisierung & Clips
docker compose --profile test run --rm --build test         # alle Tests bestanden
```

Im Dashboard (eigener Kanal oder Kanal mit schriftlicher Erlaubnis):

1. Streamer → *Neue Erlaubnis dokumentieren* (inkl. Veröffentlichung) → Modus B.
2. *Autorisierungslink erstellen* → Link im privaten Browserfenster öffnen → bei Twitch zustimmen →
   „Vielen Dank – der Zugriff ist eingerichtet“. Detailseite: Status „Verarbeitung & Veröffentlichung erlaubt“.
3. Nach dem nächsten Stream (oder sofort für einen vorhandenen Stream):

```bash
# VOD des letzten Streams zuordnen und Kandidaten planen (statt auf die Zeitsteuerung zu warten)
SID=$(docker compose exec -T postgres psql -U twitchclip -d twitchclip -tAc "select id from stream_sessions order by id desc limit 1")
docker compose exec worker python -c "from app.workers.jobs.twitch import attach_vod_job as j; print(j($SID))"
docker compose exec worker python -c "from app.workers.jobs.clips import plan_stream_clips_job as j; print(j($SID, True))"
sleep 30
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select id, source, twitch_clip_id, status, file_size, error from clip_requests order by id desc limit 10;"
docker compose exec worker-media sh -c 'ls -lh /data/downloads/*/'
```

4. Manuell: Detailseite → Stream → Startzeit `0:10:00`, Länge `30` → *Clip erstellen*. Nach ca. 1 Minute steht
   der Auftrag auf „Bereit für Analyse“; *Ansehen* spielt die Datei ab. Der Clip erscheint auch im Clip-Manager
   des Kanals.
5. *Trennen* → Status „Twitch-Autorisierung fehlt“; neue Aufträge werden abgelehnt.

Testmodus (ohne Twitch, ohne fremde Rechte):

```bash
sed -i 's/^TEST_MODE=.*/TEST_MODE=true/' .env && docker compose up -d
docker compose exec worker-media python -m app.cli make-samples --count 2 --duration 30
docker compose exec worker python -m app.cli ingest-samples      # "2 Beispieldatei(en) übernommen"
# Dashboard → Streamer → "Testmodus (Beispieldateien)" → Clip-Aufträge "Bereit für Analyse"
sed -i 's/^TEST_MODE=.*/TEST_MODE=false/' .env && docker compose up -d   # wieder Normalbetrieb
```

## Phase 4 – Abnahme auf dem VPS (KI-Highlight-Erkennung)

```bash
cd /opt/twitchclip/twitchclip-ai
git am /pfad/TwitchClip-Phase-4.patch && docker compose up -d --build   # Image enthält jetzt OpenCV + faster-whisper
docker compose logs migrate | grep 0004
docker compose --profile test run --rm --build test                   # alle Tests bestanden

# 1. Whisper-Modell vorab laden (einmalig, ~480 MB für "small")
docker compose exec worker-media python -c "from app.ai.transcribe import WhisperTranscriber; from app.config import get_settings; WhisperTranscriber(get_settings()); print('Modell bereit')"

# 2. Testmodus: Analyse mit Beispielvideos (ohne Twitch, ohne fremde Rechte)
sed -i 's/^TEST_MODE=.*/TEST_MODE=true/; s/^MIN_HIGHLIGHT_SCORE=.*/MIN_HIGHLIGHT_SCORE=30/' .env && docker compose up -d
docker compose exec worker-media python -m app.cli make-samples --count 3 --duration 60
docker compose exec worker python -m app.cli ingest-samples
sleep 60
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select id, round(score::numeric,1) as score, start_seconds, end_seconds, status, left(error,60) from highlights order by id;"
#   erwartet: je Video ein Highlight, Fenster 20–45 s um die lauten Stellen, Status "candidate"

# 3. Dashboard → Highlights → Highlight öffnen: Bewertung im Detail, KI-Analyse, Vorschau des Ausschnitts
# 4. Zurück zum Normalbetrieb
sed -i 's/^TEST_MODE=.*/TEST_MODE=false/; s/^MIN_HIGHLIGHT_SCORE=.*/MIN_HIGHLIGHT_SCORE=60/' .env && docker compose up -d
```

Mit einem autorisierten Kanal (Phase 3) läuft dieselbe Analyse automatisch nach jedem Stream; mit erlaubter
Chat-Statistik erscheint zusätzlich „Chat-Reaktion“ in der Bewertung, und Chat-Spitzen werden zu eigenen
Kandidaten (Clip-Aufträge „Aus VOD (automatisch)“).

## Phase 5 – Abnahme auf dem VPS (Videoschnitt & Untertitel)

```bash
cd /opt/twitchclip/twitchclip-ai
git am /pfad/TwitchClip-Phase-5.patch && docker compose up -d --build     # Image enthält jetzt fonts-dejavu-core
docker compose --profile test run --rm --build test                     # alle Tests bestanden
docker compose exec worker-media fc-list | grep -i dejavu               # Schrift für Untertitel vorhanden

# Testmodus (wie in Phase 4): Beispielvideos → Analyse → Auswahl → automatischer Schnitt
sed -i 's/^TEST_MODE=.*/TEST_MODE=true/; s/^MIN_HIGHLIGHT_SCORE=.*/MIN_HIGHLIGHT_SCORE=30/' .env && docker compose up -d
docker compose exec worker-media python -m app.cli make-samples --count 2 --duration 60 --overwrite
docker compose exec worker python -m app.cli ingest-samples
sleep 120
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select h.id, h.status, v.layout, v.width, v.height, round(v.duration::numeric,1) as s, pg_size_pretty(v.size_bytes) from highlights h join rendered_videos v on v.highlight_id = h.id and v.deleted_at is null order by h.id;"
#   erwartet: status rendered, 1080 × 1920, 20–45 s

# Technische Prüfung einer Ausgabedatei
F=$(docker compose exec -T postgres psql -U twitchclip -d twitchclip -tAc "select file_path from rendered_videos where deleted_at is null order by id desc limit 1")
docker compose exec worker-media ffprobe -v error -show_entries stream=codec_name,width,height,r_frame_rate,sample_rate -of compact "$F"
docker compose exec worker-media sh -c "ffmpeg -hide_banner -i '$F' -af ebur128 -f null - 2>&1 | grep ' I:' | tail -1"   # ≈ -14 LUFS

sed -i 's/^TEST_MODE=.*/TEST_MODE=false/; s/^MIN_HIGHLIGHT_SCORE=.*/MIN_HIGHLIGHT_SCORE=60/' .env && docker compose up -d
```

Im Dashboard: Highlight öffnen → Vorschau abspielen (Untertitel, Überschrift), Texte bearbeiten →
*Speichern & neu schneiden*, Layout wechseln, MP4 und SRT herunterladen.

## Phase 6 – Abnahme auf dem VPS (TikTok & Freigabe)

```bash
cd /opt/twitchclip/twitchclip-ai
git am /pfad/TwitchClip-Phase-6.patch && docker compose up -d --build
docker compose exec web alembic current                                  # 20261118_0005 (head)
docker compose --profile test run --rm --build test                       # alle Tests bestanden

# 1) Ohne TikTok-Zugang: Freigabe + manueller Export (funktioniert sofort, auch im Testmodus)
#    Dashboard: Highlight (Status „Geschnitten“) → „Für TikTok vorbereiten“ → „Prüfen & freigeben“
#    → Weg „Manueller Export“ → beide Bestätigungen → Freigeben → ZIP herunterladen
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select id, method, status, export_path is not null as zip from publish_jobs order by id desc limit 5;"
F=$(docker compose exec -T postgres psql -U twitchclip -d twitchclip -tAc "select export_path from publish_jobs where status='exported' order by id desc limit 1")
docker compose exec worker python -c "import sys, zipfile; print(zipfile.ZipFile(sys.argv[1]).namelist())" "$F"
#   erwartet: ['video.mp4', 'beschreibung.txt', 'untertitel.srt', 'HINWEISE.txt']

# 2) Mit TikTok-App (Sandbox): .env ergänzen, dann
grep -E '^TIKTOK_(CLIENT_KEY|DIRECT_POST_ENABLED|INBOX_UPLOAD_ENABLED)=' .env
docker compose up -d
#    Dashboard → TikTok-Konten → verbinden → Konto „aktiv“, Sichtbarkeiten sichtbar
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select a.display_name, t.scopes, t.expires_at, left(t.access_token, 12) as verschluesselt from tiktok_accounts a join oauth_tokens t on t.tiktok_account_id = a.id;"
#    Highlight vorbereiten → Weg „Upload in TikTok-Inbox“ (oder Direct Post mit „Nur ich“) → Freigeben
docker compose logs -f worker | grep -iE 'publish|tiktok'          # init → Upload → Status
docker compose exec -T postgres psql -U twitchclip -d twitchclip -c \
  "select id, method, status, tiktok_status, publish_id, uploaded_bytes, fail_reason from publish_jobs order by id desc limit 5;"
#    erwartet: sent_to_inbox (Benachrichtigung in der TikTok-App) bzw. published

# 3) Periodische Jobs
docker compose exec worker python -c "from app.workers.scheduler import build_periodic_jobs; from app.config import get_settings; print([j.name for j in build_periodic_jobs(get_settings()) if 'tiktok' in j.name])"
```

Prüfen im Dashboard: Prüfseite zeigt Konto-Avatar/-Name, Sichtbarkeit ist **nicht** vorausgewählt,
Kommentare/Duett/Stitch sind aus, „Inhalt kommerziell kennzeichnen“ blendet „Your brand“/„Branded content“
ein, bei „Branded content“ ist „Nur ich“ gesperrt und der Einwilligungstext wechselt. Ohne beide
Bestätigungen wird nicht freigegeben. Ein Betrachter-Konto sieht kein Freigabeformular.

## Phase 7 – Abnahme auf dem VPS (Produktionsbetrieb)

```bash
cd /opt/twitchclip/twitchclip-ai
git am /pfad/TwitchClip-Phase-7.patch && docker compose up -d --build
docker compose --profile test run --rm --build test                       # alle Tests bestanden
docker compose ps                                                         # alle Dienste running/healthy

# 1) Härtung: schreibgeschützt, ohne Capabilities, als Benutzer app
for c in web worker worker-media scheduler; do docker inspect twitchclip-$c-1 \
  --format "$c ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}} pids={{.HostConfig.PidsLimit}} user={{.Config.User}}"; done
docker compose exec web sh -c 'touch /app/x; touch /tmp/ok && touch /data/ok && echo schreiben-ok'
#   erwartet: ro=true caps=[ALL] user=app; „/app/x: Read-only file system“, dann schreiben-ok

# 2) Gesamter Ablauf im Testmodus (synthetische Videos → Analyse → Schnitt → Freigabe → ZIP)
sed -i 's/^TEST_MODE=.*/TEST_MODE=true/' .env && docker compose up -d
docker compose exec worker-media python -m app.cli selftest               # SELBSTTEST BESTANDEN (2–5 min)
docker compose exec worker-media python -m app.cli selftest --no-transcribe   # schneller, ohne Whisper
sed -i 's/^TEST_MODE=.*/TEST_MODE=false/' .env && docker compose up -d

# 3) Alarmierung: Test, Ausfall simulieren, Entwarnung
docker compose exec worker python -m app.cli test-alert
docker compose stop scheduler && sleep 200
docker compose exec worker python -c "from app.workers.jobs.maintenance import alerts_job; print(alerts_job())"
#   → Meldung „Scheduler sendet keinen Herzschlag“ am Webhook
docker compose start scheduler && sleep 15
docker compose exec worker python -c "from app.workers.jobs.maintenance import alerts_job; print(alerts_job())"
#   → „behoben: 1 Meldung(en)“
bash scripts/healthcheck.sh; echo "exit=$?"                               # 0 = alles in Ordnung

# 4) Metriken (falls METRICS_TOKEN gesetzt)
TOKEN=$(grep ^METRICS_TOKEN= .env | cut -d= -f2)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/metrics                       # 401
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/metrics | grep -E '^tcai_(workers|database_up|redis_up|scheduler)'

# 5) Verzögerte Jobs nach Neustart (Regression): Statusabfragen/Wiederholungen laufen sofort weiter
docker compose restart worker scheduler && sleep 20
docker compose exec worker python -c "
from app.redis_client import get_redis; r = get_redis()
print({k.decode(): v.decode() for k, v in ((k, r.get(k)) for k in r.keys('rq:scheduler-lock:*'))})"
#   erwartet: Sperren für default/media/publish vorhanden

# 6) Reboot-Test
sudo reboot   # danach: docker compose ps, curl -fsS http://127.0.0.1:8000/readyz
```

Sicherheitsprüfung und Befehle zur Kontrolle der Härtung: `docs/SECURITY.md`.

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
