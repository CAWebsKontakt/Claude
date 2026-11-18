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

### Streamer auf Modus B umstellen – einfachster Weg: Zustimmungslink

1. Dashboard → Streamer → Abschnitt **Zustimmung anfragen** → Haken „auch Veröffentlichung auf TikTok
   anfragen“ (und optional Chat) → **Zustimmungslink erstellen**.
2. Darunter stehen fertige Nachrichten mit Name und Link: Twitch-Flüsternachricht, Discord/E-Mail, Englisch,
   Erinnerung. Antippen markiert den Text → kopieren → an den Streamer schicken. Tipps: `docs/STREAMER_OUTREACH.md`.
3. Der Streamer öffnet den Link, sieht die Zustimmungserklärung, wählt aus (Clips erstellen ist Pflicht,
   TikTok-Veröffentlichung und Chat sind freiwillig und nicht vorausgewählt) und meldet sich bei Twitch an.
4. Nur wenn Twitch bestätigt, dass das angemeldete Konto **der Kanalinhaber** ist, wird automatisch eine
   Erlaubnis gespeichert – mit Wortlaut, Twitch-Konto, Twitch-ID und Zeitpunkt als Nachweis – und Modus B
   aktiviert. Moderatoren oder Editoren können so nicht zustimmen. Eine neue Zustimmung ersetzt die alte
   (z. B. wenn der Streamer die Veröffentlichung später abwählt).

### Streamer auf Modus B umstellen – mit schriftlicher Erlaubnis (Vertrag, E-Mail, Editor)

1. Schriftliche Erlaubnis des Streamers oder eines berechtigten Editors einholen (E-Mail, Vertrag, Formular) –
   mit Umfang: nur Verarbeitung oder auch TikTok-Veröffentlichung, ggf. Einschränkungen.
2. Dashboard → Streamer → *Neue Erlaubnis dokumentieren*: Wer, Rolle, Nachweis (Ablageort), Gültigkeit,
   Umfang, Bestätigung. Option „Danach auf Modus B umstellen“.
3. Abschnitt *Twitch-Autorisierung* → **Autorisierungslink erstellen** (optional „Chat-Statistik erlauben“).
   Den angezeigten Link an den Streamer oder einen Editor des Kanals senden (gültig `TWITCH_INVITE_VALID_DAYS`
   Tage, einmalig verwendbar). Er/sie sieht eine Erklärung aller Berechtigungen, meldet sich bei Twitch an und
   stimmt zu – ein Dashboard-Konto ist nicht nötig.
4. Der Status wechselt zu „Nur Verarbeitung erlaubt“ bzw. „Verarbeitung & Veröffentlichung erlaubt“; in der
   Tabelle steht, wer autorisiert hat (Kanalinhaber oder Editor) und wann das Token zuletzt geprüft wurde.

Zugriff beenden: *Trennen* in der Autorisierungstabelle (widerruft das Token auch bei Twitch). Trennt der
Streamer die App selbst bei Twitch, erkennt TwitchClip AI das per EventSub sofort, spätestens bei der
stündlichen Prüfung.

Widerruf: in der Erlaubnis-Tabelle *Widerrufen* (mit Grund). Ist keine gültige Erlaubnis mehr vorhanden, fällt
der Streamer **automatisch** auf Modus A zurück. Abgelaufene Erlaubnisse (Gültig bis) wirken genauso.

### Streamer entfernen

Detailseite → *Streamer entfernen* → Twitch-Namen zur Bestätigung eintippen. Löscht Nachweise, Tokens, Clips und
Highlights; das Aktivitätsprotokoll bleibt erhalten. Alternativ *Überwachung pausieren*, um Daten zu behalten.

### Hype-Liste

Dashboard → *Hype-Liste*: wachsende Live-Kanäle (Sprache/Kategorie/Zuschauerbereich per `HYPE_*` in der `.env`),
mit *Zustimmung anfragen* (Link + Nachricht, siehe `docs/STREAMER_OUTREACH.md`) oder *Nur beobachten*.
Kosten: je Lauf `HYPE_PAGES` + bis zu `HYPE_CLIP_CHECKS` Helix-Aufrufe (Standard ≤ 28 alle 15 min).

## 2. Twitch-Überwachung

* **Live-Erkennung:** EventSub-Webhooks melden Streamstart/-ende in Sekunden. Zusätzlich fragt der Scheduler alle
  `TWITCH_POLL_INTERVAL_SECONDS` den Live-Status ab (falls ein Webhook verloren geht). Ohne öffentliche
  HTTPS-Domain läuft automatisch nur das Polling.
* **Nach dem Hinzufügen** eines Streamers werden sofort Twitch-ID, Live-Status und Clips abgefragt und die
  EventSub-Abos angelegt. Detailseite → *Twitch jetzt abfragen* wiederholt das manuell.
* **Öffentliche Clips:** Dashboard → *Öffentliche Clips*: Bewertung 0–100 mit Begründung („Warum diese
  Bewertung?“), Wiedergabe im offiziellen Twitch-Player, Link zu Twitch, *Ausblenden*.
* **Status:** Dashboard → *System* → Abschnitt *Twitch* (Abos, Kosten, letzte Abfragen, API-Kontingent, letzter Fehler).

Manuell auslösen (z. B. nach Änderung der Domain):

```bash
docker compose exec worker python -c "from app.workers.jobs.twitch import sync_eventsub_job as j; print(j())"
docker compose exec worker python -c "from app.workers.jobs.twitch import poll_live_status_job as j; print(j())"
docker compose exec worker python -c "from app.workers.jobs.twitch import discover_clips_job as j; print(j())"
```

| Problem | Lösung |
|---|---|
| „Twitch-Konto … nicht gefunden“ | Login falsch geschrieben oder Konto gesperrt → Streamer entfernen und korrekt neu anlegen |
| Abos bleiben `webhook_callback_verification_pending`/`failed` | `https://<domain>/webhooks/twitch/eventsub` muss von außen über 443 erreichbar sein; `docker compose logs web \| grep EventSub` |
| System zeigt „nur Polling“ | `APP_BASE_URL` muss `https://` ohne Port sein, `TWITCH_EVENTSUB_SECRET` gesetzt, `TWITCH_EVENTSUB_ENABLED=true` |
| „EventSub-Kostenlimit erreicht“ | mehr als ca. 5.000 Kanäle ohne Autorisierung – überzählige laufen per Polling |
| „App-Token abgelehnt“ | `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET` prüfen, Secret ggf. in der Twitch-Konsole neu erzeugen |
| Clip-Auftrag „Fehlgeschlagen … HTTP 404“ | Pfad des Clip-Endpunkts geändert? `TWITCH_VOD_CLIP_PATH`/`TWITCH_CLIP_DOWNLOAD_PATH` mit der Twitch-Referenz abgleichen |
| „Zugriff verweigert (403)“ beim Editor | Das autorisierende Konto ist kein Editor des Kanals → Streamer selbst autorisieren lassen |
| Kein VOD gefunden | Beim Streamer muss „Vergangene Übertragungen speichern“ aktiviert sein |
| Highlights ohne Transkript („Sprache … nicht verfügbar“) | Whisper-Modell nicht ladbar (huggingface.co erreichbar?) – `docker compose logs worker-media \| grep Spracherkennung` |
| Media-Worker wird beendet (OOM) | `MEDIA_WORKER_MEMORY` erhöhen oder kleineres `WHISPER_MODEL` wählen |
| „Schnitt fehlgeschlagen: FFmpeg-Fehler …“ | Meldung im Highlight lesen; Quelldatei beschädigt oder bereits gelöscht (Aufbewahrungsfrist) |
| Untertitel fehlen im Video | Kein Transkript (Whisper) oder `SUBTITLES_ENABLED=false`; Schrift fehlt → Image neu bauen (`fonts-dejavu-core`) |

## 3. Clip-Verarbeitung (Modus B)

* **Automatisch:** Nach jedem Stream wird das VOD zugeordnet; 15 Minuten später werden die besten,
  sich nicht überlappenden Zuschauer-Clips (max. `CLIP_CANDIDATES_PER_STREAM`) übernommen und heruntergeladen.
* **Manuell:** Detailseite → *Streams & Clip-Verarbeitung* → Startzeit (z. B. `1:02:03`) und Länge (5–60 s) →
  *Clip erstellen*. Twitch erzeugt den Clip im Kanal; der Download folgt automatisch.
* **Status je Auftrag:** Geplant → (Wird erstellt → Twitch verarbeitet) → Wird geladen → **Bereit für Analyse**.
  Übersprungen = Rechte fehlten zum Zeitpunkt des Schritts; Fehlgeschlagen = nach allen Wiederholungen.
  Beide lassen sich mit *Erneut versuchen* neu starten (die Rechte werden dabei erneut geprüft).
* **Dateien:** `/data/downloads/<streamer-id>/clip-<auftrag>.mp4`, Vorschau über *Ansehen*; Löschung nach
  `STORAGE_RETENTION_DAYS`.

### Testmodus mit Beispieldateien (ohne fremde Rechte)

```bash
# 1. In .env: TEST_MODE=true  → docker compose up -d
# 2. Synthetische Testvideos erzeugen (Testbild + „Facecam“ + Ton mit lauten Stellen, keine Fremdinhalte)
docker compose exec worker-media python -m app.cli make-samples --count 3 --duration 60
# 3. Sofort übernehmen (sonst automatisch alle 5 Minuten)
docker compose exec worker python -m app.cli ingest-samples
```

Eigene Videos, an denen Sie alle Rechte haben, können ebenfalls nach `/data/samples` kopiert werden
(`docker compose cp mein-video.mp4 worker:/data/samples/`). Sie erscheinen beim Demo-Kanal „Testmodus
(Beispieldateien)“. Im Testmodus ruft das System **keine** Twitch-API auf.

## 4. KI-Highlight-Erkennung

* Jeder heruntergeladene Clip wird automatisch analysiert (Media-Worker). Ergebnis: Dashboard → *Highlights*
  mit Bewertung 0–100, Begründung, Transkript, erkannter Facecam und Vorschau des gewählten Ausschnitts.
* Je Stream bleiben die besten 3–5 als „Kandidat“ stehen; die übrigen werden mit Begründung verworfen
  („unter MIN_HIGHLIGHT_SCORE“, „nicht unter den besten 5“, „Tageslimit“). Verwerfen/Wiederherstellen per Hand
  ist jederzeit möglich.
* **Whisper-Modell:** wird beim ersten Analysejob automatisch nach `/data/models` geladen (`small` ≈ 480 MB,
  benötigt Zugriff auf huggingface.co). Vorab laden:

  ```bash
  docker compose exec worker-media python -c "from app.ai.transcribe import WhisperTranscriber; from app.config import get_settings; WhisperTranscriber(get_settings()); print('Modell bereit')"
  ```

  Kleinere Server: `WHISPER_MODEL=base` oder `tiny`; ganz ohne Spracherkennung: `ANALYSIS_TRANSCRIPTION=false`.
* **Testmodus:** Beispielvideos haben keinen Chat und keine Zuschauer-Signale; für eine aussagekräftige
  Vorschau `MIN_HIGHLIGHT_SCORE=30` setzen.
* Analyse manuell anstoßen:

  ```bash
  docker compose exec worker python -c "from app.workers.jobs.analysis import analyze_pending_job as j; print(j())"
  ```

## 5. Videoschnitt und Untertitel

* Ausgewählte Highlights (Phase 4) werden automatisch im Hochformat 1080×1920 geschnitten. Ergebnis:
  Dashboard → *Highlights* → Highlight → Vorschau mit Untertiteln, *MP4 herunterladen*, *Untertitel (SRT)*.
* **Texte bearbeiten:** Überschrift (im Video eingeblendet, max. 60 Zeichen), TikTok-Beschreibung, bis zu
  5 Hashtags → *Speichern* oder *Speichern & neu schneiden*. Die Vorschau zeigt den späteren TikTok-Text.
* **Layout ändern:** *Layout* → „Facecam oben“ bzw. „Gameplay mittig“ → *Neu schneiden*.
* Rechenzeit (4 vCPU, `RENDER_PRESET=medium`): etwa 20–40 s je 30-s-Clip. Auf kleinen Servern
  `RENDER_PRESET=veryfast` setzen.

## 6. TikTok-Veröffentlichung

1. **Konto verbinden:** *TikTok-Konten* → *TikTok-Konto verbinden* (siehe `API_REGISTRATION.md` 2.4).
2. **Vorbereiten:** Highlight öffnen → *Für TikTok vorbereiten*. Möglich nur bei fertig geschnittenem Video und
   gültiger Veröffentlichungserlaubnis des Streamers (Modus B, `allows_publishing`).
3. **Prüfen & freigeben:** *TikTok-Warteschlange* → *Prüfen & freigeben*. Die Seite fragt die Kontodaten live
   bei TikTok ab. Weg wählen, Beschreibung prüfen, bei Direct Post Sichtbarkeit und Interaktionen wählen,
   ggf. Werbung kennzeichnen, Erlaubnis und Einwilligung bestätigen → *Freigeben*.
4. **Status:** Wird hochgeladen → TikTok verarbeitet → *Veröffentlicht* (Link zum Post) bzw. *In TikTok-Inbox*
   (in der App fertigstellen). *Status jetzt abfragen* erzwingt eine sofortige Abfrage.
5. **Export:** Weg *Manueller Export* → ZIP herunterladen → in der TikTok-App hochladen.
6. **Fehler:** Die Ursache steht im Auftrag (z. B. „Tageslimit für Posts erreicht“, „Konto neu verbinden“).
   *Erneut vorbereiten* stellt den Auftrag zurück zur Prüfung – es wird nie automatisch neu gepostet.

| Meldung | Ursache / Lösung |
|---|---|
| „unaudited_client …“ / nur privat | App nicht zugelassen → „Nur ich“ wählen, Inbox-Upload oder Export nutzen |
| „Konto neu verbinden“ | Token widerrufen/abgelaufen → *TikTok-Konten* → neu verbinden |
| „Sichtbarkeit nicht mehr verfügbar“ | Kontoeinstellungen bei TikTok geändert → *Erneut vorbereiten* |
| bleibt „TikTok verarbeitet“ | normal bis einige Minuten; nach 48 h wird der Auftrag als fehlgeschlagen markiert |

## 7. Benutzer

* Dashboard → *Benutzer* (nur Admins): anlegen, Rolle ändern, deaktivieren, Passwort setzen.
* Rollen: **Administrator** (alles), **Redakteur** (Streamer, Rechte, Highlights, Freigaben), **Betrachter** (lesen).
* Passwort vergessen:

  ```bash
  docker compose exec web python -m app.cli reset-password --username admin
  ```

## 8. Überwachung

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
| Alarme | Dashboard → *System* → „Alarme“ (Ergebnis der Prüfung alle 5 min, *Jetzt prüfen*); `python -m app.cli check-alerts` |
| Benachrichtigung | `ALERT_WEBHOOK_URL` in der `.env` (Discord, Slack oder Klartext, z. B. <https://ntfy.sh> aufs Handy); Test: `python -m app.cli test-alert` |
| Außenprüfung | `scripts/healthcheck.sh` per Cron (INSTALL.md Abschnitt 4) – meldet auch Ausfälle des Schedulers/der Container |
| Metriken | `METRICS_TOKEN` setzen → `curl -H "Authorization: Bearer $TOKEN" https://<domain>/metrics` (Prometheus/Grafana) |
| Selbsttest | `TEST_MODE=true`, dann `docker compose exec worker-media python -m app.cli selftest` |

Gemeldet werden: kein Worker, Scheduler ohne Herzschlag, viele fehlgeschlagene/wartende Jobs, Speicher knapp,
Twitch-Autorisierung fehlt/widerrufen, Erlaubnis läuft in ≤ 7 Tagen ab, TikTok-Konto muss neu verbunden werden,
TikTok-Auftrag fehlgeschlagen, Videos warten > 3 Tage auf Freigabe, ≥ 5 fehlgeschlagene Clip-Aufträge in 24 h.
Jede Meldung kommt einmal (Wiederholung nach `ALERT_REPEAT_HOURS`), behobene Probleme werden entwarnt.

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

## 9. Speicher

* Automatisch: stündliche Bereinigung (`STORAGE_RETENTION_DAYS`, `STORAGE_MAX_GB`), Videos in offener Freigabe
  bleiben geschützt. Docker-Logs sind auf 5 × 10 MB je Container begrenzt.
* Manuell:

  ```bash
  docker compose exec worker python -m app.cli cleanup-storage --dry-run   # anzeigen
  docker compose exec worker python -m app.cli cleanup-storage             # löschen
  docker image prune -f                                                    # alte Images
  ```

## 10. Sicherung und Wiederherstellung

```bash
bash scripts/backup.sh                                     # sofortiges Backup nach ./backups
bash scripts/restore.sh backups/twitchclip-YYYYMMDD-HHMMSS.sql.gz
```

Zu sichern sind: `backups/*.sql.gz` **und** die `.env` (enthält `TOKEN_ENCRYPTION_KEY`) – getrennt voneinander.

## 11. Schlüssel rotieren

* `SECRET_KEY` ändern → alle Benutzer werden abgemeldet. `docker compose up -d`.
* `TOKEN_ENCRYPTION_KEY` rotieren:

  ```bash
  NEW=$(docker compose exec -T web python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" | tr -d '\r')
  # in .env: TOKEN_ENCRYPTION_KEY=<NEU>,<ALT>
  docker compose up -d
  docker compose exec web python -m app.cli rotate-token-key
  # danach in .env nur noch TOKEN_ENCRYPTION_KEY=<NEU>, erneut docker compose up -d
  ```

## 12. Fehlerbehebung

| Symptom | Ursache / Lösung |
|---|---|
| `migrate` exited (1) | `docker compose logs migrate` – meist falsches `POSTGRES_PASSWORD` nach Änderung (Passwort ist beim ersten Start in das Volume geschrieben) |
| `web` startet nicht, „SECRET_KEY fehlt“ | `.env` prüfen: `docker compose run --rm --no-deps web python -m app.cli check-config` |
| Kein HTTPS-Zertifikat | DNS zeigt nicht auf den Server oder Port 80/443 blockiert: `docker compose logs caddy` |
| Login „Zu viele Fehlversuche“ | 15 Minuten warten oder `docker compose exec redis sh -c "redis-cli --scan --pattern 'tcai:login-fail:*' \| xargs -r redis-cli del"` |
| System-Seite: „Kein RQ-Worker aktiv“ | `docker compose up -d worker worker-media`, Logs prüfen |
| System-Seite: „Scheduler sendet keinen Herzschlag“ | `docker compose restart scheduler` |
