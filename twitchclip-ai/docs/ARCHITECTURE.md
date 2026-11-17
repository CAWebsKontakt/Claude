# TwitchClip AI – Architektur

## 1. Leitprinzipien

1. **Rechte vor Technik.** Jeder Verarbeitungsschritt (Download, Analyse, Schnitt, Upload) prüft unmittelbar
   vor der Ausführung `compute_rights()` (`app/services/streamers.py`). Fehlt etwas, wird der Schritt nicht
   ausgeführt – unabhängig davon, was in der Warteschlange steht.
2. **Nur offizielle APIs.** Keine inoffiziellen Download-URLs, keine GraphQL-Schnittstellen der Twitch-Website,
   keine Browserautomatisierung für TikTok.
3. **Mensch gibt frei.** Nichts wird ohne ausdrückliche Freigabe im Dashboard auf TikTok hochgeladen.
4. **Begrenzte Ressourcen.** Ein Medien-Worker mit Concurrency 1, harte Speicherobergrenze, automatische
   Löschung, Speicher-/CPU-Limits je Container.
5. **Idempotenz.** Jeder Job hat eine stabile ID; Clips/Highlights haben eindeutige Schlüssel
   (`processing_keys`, `content_hash`, `twitch_clip_id`, `publish_id`). Wiederholungen erzeugen nie Duplikate.

## 2. Komponenten

```
                    Internet
                       │  443/80 (HTTP/3, Let's Encrypt)
                ┌──────▼──────┐
                │    Caddy    │  TLS, HSTS, Kompression, Reverse-Proxy
                └──────┬──────┘
          egress-Netz  │                        Twitch Helix / EventSub     TikTok Open API
     ┌─────────────────▼──────────────┐                ▲      │                    ▲
     │ web  (FastAPI + Jinja2, 2 Proc)│  Webhooks ◄─────┘      │                    │
     │  Dashboard, Login, OAuth-       │  (EventSub)            │                    │
     │  Callbacks, Medienauslieferung  │                        │                    │
     └───────┬───────────────┬────────┘                        │                    │
             │               │ Jobs einreihen                   │                    │
     ┌───────▼───┐     ┌─────▼─────┐   ┌───────────────┐  ┌─────┴──────┐  ┌──────────┴─┐
     │ PostgreSQL│◄────┤   Redis   ├──►│   scheduler   │  │   worker   │  │worker-media│
     │  (Daten)  │     │ (RQ-Queues│   │ periodische   │  │ default +  │  │ media      │
     └───────────┘     │  Locks,   │   │ Jobs, Locks   │  │ publish    │  │ Whisper,   │
       backend-Netz    │  Limits)  │   └───────────────┘  │ (API-Jobs) │  │ FFmpeg,    │
       (kein Internet) └───────────┘                      └────────────┘  │ OpenCV     │
                                                                          └─────┬──────┘
                                                               Volume media_data│ /data
                                                     tmp/ downloads/ renders/ exports/ samples/
```

| Dienst | Aufgabe | Limit (Standard) |
|---|---|---|
| `caddy` | HTTPS, Reverse-Proxy | 128 MB |
| `web` | Dashboard, Login, Webhooks, OAuth-Callbacks, Videoauslieferung (Range-Requests) | 512 MB |
| `worker` | Queues `default` + `publish`: Twitch-API, Clip-Suche, Token-Refresh, TikTok-Upload/Status, Wartung | 512 MB |
| `worker-media` | Queue `media`: Clip-Downloads, Whisper, FFmpeg-Analyse und -Schnitt (immer nur ein Job) | 3 GB (`MEDIA_WORKER_MEMORY`) |
| `scheduler` | Reiht periodische Jobs ein (Redis-Lock pro Intervall, mehrfach startbar ohne Doppelungen) | 192 MB |
| `migrate` | Einmal je `up`: Alembic-Migrationen, Verzeichnisse, `STREAMER_LIST`-Import | 256 MB |
| `postgres` | Datenhaltung (16-alpine) | 512 MB |
| `redis` | RQ-Queues, Locks, Rate-Limit-Zähler (`noeviction`, AOF) | 256 MB |

Alle Dienste: `restart: unless-stopped` → automatischer Start nach Reboot (Docker-Dienst ist per systemd aktiviert).
PostgreSQL und Redis hängen nur im internen Netz `backend` ohne Internetzugang.

## 3. Betriebsarten

### Modus A – Öffentliche Kanäle (Standard für jeden neuen Streamer)

```
EventSub stream.online/offline ──► Live-Status, StreamSession
Polling Get Streams (Fallback)  ──►
Get Clips (Helix, App-Token)    ──► twitch_clips (nur Metadaten) ──► Bewertung ──► Dashboard: Link, Vorschaubild,
                                                                                  offizieller Embed-Player, Empfehlung
```

* Gespeichert werden ausschließlich Metadaten (Titel, Aufrufe, Dauer, Zeitpunkt, Vorschaubild-URL, Clip-Link).
* **Kein** Download, **keine** Bearbeitung, **keine** Veröffentlichung. Die Pipeline-Jobs ab Phase 3 lehnen
  Streamer in Modus A hart ab (`RightsStatus.PUBLIC_ONLY`).
* Bewertung (Phase 2): Aufrufe pro Stunde seit Erstellung (normiert auf die Kanalgröße), Aktualität (Zerfall),
  Featured-Flag, Länge im Zielbereich 20–45 s, Clip-Dichte im selben VOD-Zeitfenster (mehrere Zuschauer haben
  denselben Moment geclippt = starkes Signal).

### Modus B – Autorisierte Streamer

Voraussetzungen (alle drei, laufend geprüft):

1. **Dokumentierte Erlaubnis** (`streamer_permissions`): wer, Rolle, Nachweis, Umfang (Verarbeitung /
   Veröffentlichung), Gültigkeit; Bestätigung, dass sie schriftlich vorliegt. Widerruf → automatisch Modus A.
2. **Twitch-OAuth** des Streamers oder eines Channel-Editors mit `editor:manage:clips` oder
   `channel:manage:clips` (Phase 3).
3. Für TikTok zusätzlich: Erlaubnis umfasst Veröffentlichung **und** ein verbundenes TikTok-Konto (Phase 6).

Pipeline (Phasen 3–6):

```
 Signale sammeln                    Kandidaten                 Offizielle Clip-Erstellung      KI-Analyse
 ───────────────                    ──────────                 ─────────────────────────       ──────────
 Chat-Aktivität (EventSub,  ─┐      Zeitfenster mit       ─►   Create Clip From VOD      ─►   faster-whisper (Transkript)
  aggregiert, ohne Inhalte)  │      hoher Signaldichte          (vod_id, vod_offset,           FFmpeg ebur128/astats (Lautheit,
 Zuschauer-Clips (vod_offset)├─►   (max. N je Stream,           duration 20–60 s)              Peaks, Lachen/Schreie-Heuristik)
 Stream-Marker               │      Abstand ≥ 90 s)        ─►   Get Clips Download        ─►   OpenCV (Szenenwechsel, Bewegung,
 Live-Ereignisse (Raids…)   ─┘                                  (temporäre MP4-URL)            Gesichter/Facecam)
                                                                                                    │
                                                                                                    ▼
  TikTok (Phase 6) ◄── Freigabe im Dashboard ◄── Rendering 1080×1920 (Phase 5) ◄── Score 0–100, Duplikatfilter,
                                                  Untertitel, Titel, Hashtags        Top 3–5 je Stream, MAX_CLIPS_PER_DAY
```

**Warum dieser Umweg über Clips?** Twitch bietet keine offizielle API zum Herunterladen kompletter VODs.
Offiziell vorgesehen sind „Create Clip From VOD“ (Clip aus einem VOD-Zeitpunkt erzeugen) und „Get Clips Download“
(Download-URL eines Clips für Broadcaster/Editoren). TwitchClip AI erzeugt daher gezielt wenige Clips an
vielversprechenden Stellen, lädt nur diese herunter und analysiert sie. Nebeneffekt: Datenmenge und Rechenzeit
bleiben auf einem VPS beherrschbar.

### Testmodus (`TEST_MODE=true`)

Verarbeitet ausschließlich Dateien in `/data/samples` (selbst erzeugte Testvideos, z. B. per FFmpeg-Testbild +
Testton, oder eigene Aufnahmen, an denen Sie die Rechte haben) über einen internen Demo-Streamer. Es werden keine
Twitch-/TikTok-Aufrufe ausgeführt; TikTok-Uploads werden simuliert bzw. als manueller Export erzeugt. Damit lässt
sich die komplette Pipeline (Analyse → Schnitt → Untertitel → Freigabe) ohne fremde Rechte testen (ab Phase 3–5).

## 4. Datenmodell (Migration `0001`)

| Tabelle | Inhalt / wichtige Eindeutigkeiten |
|---|---|
| `users` | Login, Rolle (admin/editor/viewer), Argon2id-Hash, `session_version` (Sitzungen widerrufen) |
| `streamers` | Twitch-Login (unique), Twitch-ID (unique), Modus, Live-Status |
| `streamer_permissions` | Erlaubnis-Nachweise mit Gültigkeit und Widerruf |
| `oauth_tokens` | Twitch-/TikTok-Tokens, **verschlüsselt** (Fernet), unique (provider, external_user_id) |
| `tiktok_accounts` | verbundene Konten + zuletzt abgefragte Creator-Info |
| `eventsub_subscriptions` | Abos je Streamer (unique Twitch-Subscription-ID) |
| `stream_sessions` | einzelne Streams (unique Twitch-Stream-ID) |
| `twitch_clips` | öffentliche Clip-Metadaten (unique Clip-ID), Bewertung |
| `chat_activity` | nur Zählwerte je 10-s-Fenster (unique streamer+bucket) – keine Nachrichten, keine Namen |
| `highlights` | Kandidaten, Score 0–100 + Aufschlüsselung, Transkript, `content_hash` (unique), `perceptual_hash` |
| `rendered_videos` | fertige 1080×1920-MP4, Größe, SHA-256, Löschzeitpunkt |
| `publish_jobs` | TikTok-Warteschlange inkl. Nutzerentscheidungen, Freigabe, `publish_id` (unique), Status |
| `processing_keys` | generische Idempotenz-Schlüssel („genau einmal verarbeiten“) |
| `activity_log` | Aktivitäts- und Fehlerprotokoll (Aufbewahrung `ACTIVITY_RETENTION_DAYS`) |

Alle Zeitstempel werden in UTC gespeichert und im Dashboard in `TIMEZONE` (Europe/Berlin) angezeigt.
`MAX_CLIPS_PER_DAY` bezieht sich auf den Kalendertag in dieser Zeitzone.

## 5. Hintergrundverarbeitung

* **Queues:** `default` (leicht, API), `media` (schwer, Concurrency 1), `publish` (TikTok).
* **Idempotente Jobs:** `enqueue_unique()` reiht nicht ein, wenn ein Job mit gleicher ID wartet oder läuft.
  Job-IDs sind deterministisch (`make_job_id("vodclip", vod_id, offset)`).
* **Wiederholungen:** exponentielles Backoff (1 min → 5 min → 15 min); bei HTTP 429 wird `Ratelimit-Reset`
  (Twitch) bzw. die Antwort von TikTok respektiert. Fehlgeschlagene Jobs bleiben 7 Tage in der Failed-Registry und
  erscheinen auf der Systemseite.
* **Periodische Jobs** (`app/workers/scheduler.py`): Phase 1 Speicherbereinigung (stündlich) und
  Protokollrotation (täglich); ab Phase 2 Live-Status-Abgleich, Clip-Suche, EventSub-Abgleich, Token-Refresh;
  ab Phase 6 TikTok-Statusabfragen.
* **Rate-Limits:** zentraler Token-Bucket in Redis je API und Token (Phase 2), damit auch mehrere Worker
  gemeinsam unter dem Limit bleiben.

## 6. Speicher-Lebenszyklus

| Verzeichnis | Inhalt | Löschung |
|---|---|---|
| `/data/tmp` | Zwischendateien (WAV, Frames) | nach 6 h |
| `/data/downloads` | heruntergeladene Clips (Modus B) | nach `STORAGE_RETENTION_DAYS` |
| `/data/renders` | fertige Videos, Vorschaubilder, Untertitel | nach `STORAGE_RETENTION_DAYS`, **außer** sie stecken in Freigabe/Upload |
| `/data/exports` | manuelle Export-Pakete | nach `STORAGE_RETENTION_DAYS` |
| `/data/samples` | Beispieldateien für den Testmodus | nie automatisch |

Zusätzlich harte Obergrenze `STORAGE_MAX_GB`: älteste ungeschützte Dateien zuerst (tmp → downloads → exports →
renders). Videos werden nie per Backup gesichert – sie sind reproduzierbar bzw. temporär.

## 7. Sicherheit

* Sessions: signiertes Cookie (`HttpOnly`, `Secure`, `SameSite=Lax`), max. 12 h, Widerruf über `session_version`.
* Passwörter: Argon2id, Mindestlänge 12, Login-Sperre nach Fehlversuchen (pro Benutzer und pro IP, Redis).
* CSRF-Token für jedes Formular; Open-Redirect-Schutz beim Login.
* Content-Security-Policy ohne Inline-Skripte, `frame-ancestors 'none'`, HSTS (Caddy).
* OAuth-Tokens nur verschlüsselt in der DB (Fernet, Schlüsselrotation über `rotate-token-key`).
* Container laufen als unprivilegierter Benutzer, Code ist read-only, `no-new-privileges`.
* Datenbank/Redis ohne Internetzugang, Web-Port nur an `127.0.0.1` gebunden.
* EventSub-Webhooks (Phase 2): HMAC-SHA256-Prüfung, Zeitstempel ≤ 10 min, Duplikatschutz über Message-ID.
* Datenschutz: Chat nur als anonyme Zählwerte; Aktivitätsprotokoll mit Aufbewahrungsfrist.

## 8. Projektstruktur

`✓` = vorhanden (Phase 1), Zahl = geplante Phase.

```
twitchclip-ai/
├── Dockerfile                    ✓ ein Image für alle Dienste (non-root, FFmpeg)
├── docker-compose.yml            ✓ Caddy, Web, Worker, Media-Worker, Scheduler, PostgreSQL, Redis
├── .env.example                  ✓ sichere Vorlage (keine echten Geheimnisse)
├── requirements*.txt, pyproject.toml, alembic.ini   ✓
├── deploy/Caddyfile              ✓ HTTPS + Reverse-Proxy
├── migrations/                   ✓ Alembic (0001 initiales Schema)
├── scripts/                      ✓ install_vps, init_env, backup, restore, healthcheck, update
├── docs/                         ✓ Architektur, API-Grenzen, Installation, Registrierung, Betrieb, Tests
├── tests/                        ✓ pytest (SQLite/PostgreSQL, fakeredis, später respx-Mocks)
└── app/
    ├── config.py, db.py, models.py, enums.py, timeutil.py, logging_setup.py, redis_client.py   ✓
    ├── cli.py                    ✓ Verwaltung (Benutzer, Streamer, Secrets, Bereinigung, Key-Rotation)
    ├── main.py                   ✓ FastAPI-App
    ├── security/                 ✓ crypto (Fernet), passwords (Argon2), csrf, ratelimit
    ├── services/                 ✓ activity, users, streamers (Rechte), permissions, storage, system
    ├── workers/                  ✓ queues, worker, scheduler, jobs/maintenance
    │   └── jobs/twitch.py        2  Streamer auflösen, Live-Status, Clip-Suche, EventSub-Abgleich
    │   └── jobs/clips.py         3  Clip aus VOD erzeugen, Download, Rechteprüfung
    │   └── jobs/analysis.py      4  KI-Analyse und Auswahl
    │   └── jobs/render.py        5  Schnitt, Untertitel, Metadaten
    │   └── jobs/publish.py       6  TikTok-Upload und Statusverfolgung
    ├── integrations/twitch/      2/3 Helix-Client, App-/User-OAuth, EventSub, Rate-Limit
    ├── integrations/tiktok/      6  OAuth (PKCE), Creator-Info, Direct Post / Inbox-Upload, Status
    ├── ai/                       4  audio, transcribe (faster-whisper), vision (OpenCV), chat_signals, scoring, dedup
    ├── media/                    5  layout (Facecam/Gameplay), subtitles (ASS), render (FFmpeg), metadata
    └── web/                      ✓ routes/, templates/, static/ (mobiloptimiert, ohne externe CDNs)
        └── routes/webhooks.py    2  EventSub-Endpunkt
        └── routes/oauth.py       3/6 OAuth-Callbacks Twitch/TikTok
```

## 9. Empfohlene Servergröße

| Nutzung | vCPU | RAM | SSD |
|---|---|---|---|
| nur Modus A (Monitoring, Empfehlungen) | 2 | 2–4 GB | 20 GB |
| Modus B, Whisper `small` auf CPU, wenige Streamer | 4 | 8 GB (+4 GB Swap) | 80 GB |
| Modus B, viele Streamer / Whisper `medium` | 8 | 16 GB | 160 GB |

faster-whisper `small` mit `int8` benötigt auf CPU etwa 1–2 GB RAM und verarbeitet einen 45-s-Clip auf
4 vCPU in grob 10–30 s.
