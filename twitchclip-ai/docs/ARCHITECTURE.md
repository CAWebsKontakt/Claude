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

### Modus B im Detail (Phase 3)

```
Redakteur: Erlaubnis dokumentieren ─► Autorisierungslink (signiert, 7 Tage, einmalig)
Streamer/Editor: Zustimmungsseite ─► Twitch-Login ─► Callback ─► Token-Tausch + /oauth2/validate
                 Rolle = Kanalinhaber (user_id = Kanal) oder Editor ─► Token verschlüsselt speichern
stream.offline ─► VOD zuordnen ─► (15 min) Zuschauer-Clips aktualisieren ─► beste, nicht überlappende
                 Kandidaten (max. CLIP_CANDIDATES_PER_STREAM) ─► Get Clips Download ─► /data/downloads
Dashboard: Ausschnitt anfordern ─► Create Clip From VOD ─► (30 s) Download
```

* Jeder Schritt ruft unmittelbar vorher `ensure_can_process()` auf. Widerruf (Dashboard, EventSub
  `user.authorization.revoke`, stündliche Validierung, abgelehnte Erneuerung) stoppt die Verarbeitung sofort.
* Downloads: nur HTTPS, Größenlimit `CLIP_DOWNLOAD_MAX_MB`, Teil-Dateien werden immer entfernt, SHA-256 gegen
  Duplikate, ffprobe prüft Dauer/Auflösung/Ton.
* Twitch-Fehler 403 bei einem Editor-Token → Token wird gesperrt (Konto ist offenbar kein Editor).

### KI-Highlight-Erkennung (Phase 4)

Je heruntergeladenem Clip (Media-Worker, ein Job gleichzeitig, ca. 3 s Rechenzeit je 60 s Video ohne Whisper):

| Signal | Verfahren | Ergebnis je Sekunde |
|---|---|---|
| Ton | FFmpeg → 16-kHz-Mono-WAV, RMS-Pegel in 0,5-s-Fenstern (numpy), Abstand zum Median + plötzlicher Anstieg | Aufregung 0–1, Ausbrüche (Schreie, Jubel, Lachen) |
| Sprache | faster-whisper (lokal, `WHISPER_MODEL`, int8), Wort-Zeitstempel; Schlüsselwörter (de/en), Ausrufe, Lachen, GROSSSCHREIBUNG | Aufregung 0–1, Wörter für Untertitel |
| Bild | OpenCV, 2 Bilder/s: Bewegung (Frame-Differenz), Szenenwechsel (Histogramm), Facecam (Haar-Gesichtserkennung, stabile Position) | Bewegung 0–1, Facecam-Box |
| Chat | EventSub `channel.chat.message` → Zähler je 10 s (Nachrichten, verschiedene Absender, Emotes, Hype-Wörter) | Verhältnis zum Median des Streams |
| Zuschauer | Bewertung des Zuschauer-Clips aus Phase 2 | 0–1 |

* **Fensterwahl:** gleitendes Fenster der Länge `CLIP_DURATION_MAX` über `0,45·Ton + 0,35·Sprache + 0,2·Bild`
  (Spitzen stärker gewichtet), ruhige Anfangssekunden werden bis `CLIP_DURATION_MIN` abgeschnitten.
* **Bewertung 0–100:** Ton 25 %, Sprache 20 %, Chat 20 %, Bild 15 %, Zuschauer 10 %, Länge 10 %. Fehlende
  Signale (z. B. kein Chat-Zugriff, Whisper aus) werden auf die vorhandenen umverteilt und im Dashboard als
  „nicht verfügbar“ ausgewiesen.
* **Duplikate:** gleicher VOD-Bereich (> 50 % Überlappung) oder gleiche Bild-Hashes (dHash bei 25/50/75 %,
  Abstand ≤ `DUPLICATE_HASH_DISTANCE`) **und** gleicher Lautheitsverlauf (Korrelation ≥ 0,85); 14 Tage rückwirkend.
* **Auswahl:** je Stream die besten `HIGHLIGHTS_PER_STREAM_MAX` mit Bewertung ≥ `MIN_HIGHLIGHT_SCORE`, höchstens
  `MAX_CLIPS_PER_DAY` pro Kalendertag; erst wenn alle Clips des Streams analysiert sind. Manuell verworfene
  Highlights bleiben verworfen.
* **Chat-Spitzen** (≥ `CHAT_SPIKE_FACTOR` × Median), die kein Zuschauer-Clip abdeckt, werden als zusätzliche
  Kandidaten per Create Clip From VOD erzeugt.

### Videoschnitt und Untertitel (Phase 5)

```
ausgewählte Highlights ─► Layout wählen ─► Überschrift/Beschreibung/Hashtags ─► ASS-Untertitel ─► FFmpeg ─► Prüfung ─► Vorschaubild
```

| Layout | Wann | Aufbau (1080×1920) |
|---|---|---|
| `facecam_split` | Facecam erkannt (stabile Gesichtsposition, Phase 4) | Facecam oben (480–760 px hoch, auf das Gesicht zentriert), Gameplay darunter, von der Facecam weg verschoben |
| `blur_fill` | keine Facecam | Gameplay mittig (760 px hoch, leicht gezoomt), unscharfer Hintergrund, Überschrift im freien Bereich darüber |
| `vertical` | Quelle bereits hochkant | eingepasst, Ränder unscharf gefüllt |

* **Untertitel:** aus den Whisper-Wort-Zeitstempeln, max. 3 Wörter/22 Zeichen je Zeile, DejaVu Sans Bold 80 px,
  dicke Kontur, das gesprochene Wort wird gelb (ASS-Karaoke). Zusätzlich als `.srt` zum Herunterladen.
* **Überschrift:** max. 60 Zeichen; bei `blur_fill` dauerhaft, sonst in den ersten 3,5 s.
* **Ton:** nur Original-Ton, `loudnorm` auf −14 LUFS / −1,5 dBTP, AAC 160 kbit/s 48 kHz Stereo; ohne Tonspur
  wird Stille eingefügt. Es wird **keine Musik** hinzugefügt.
* **Video:** H.264 High (yuv420p, 30 fps, GOP 2 s, `RENDER_PRESET`/`RENDER_CRF`), `+faststart` für sofortiges
  Abspielen. Jede Ausgabe wird per ffprobe geprüft (1080×1920, H.264, AAC), sonst gilt der Schnitt als
  fehlgeschlagen.
* **Texte:** Überschrift aus dem Zuschauer-Clip-Titel oder dem aufregendsten Ausruf im Transkript, Beschreibung
  mit Quellenangabe (`twitch.tv/<kanal>`), max. 5 Hashtags (Kanal, Spiel, twitch, gaming, Sprache). Bearbeitete
  Texte werden nie automatisch überschrieben.
* Ältere Fassungen werden beim Neuschnitt gelöscht – außer sie stecken bereits in der TikTok-Warteschlange.

### TikTok-Veröffentlichung (Phase 6)

```
gerendert ─► „Für TikTok vorbereiten“ ─► pending_review ─► Prüfseite (Creator-Info live) ─► Freigabe ─► approved
approved ─► Rechte + Creator-Info erneut prüfen ─► init (publish_id) ─► uploading ─► processing ─► published | sent_to_inbox | failed
approved (Export) ─► ZIP ─► exported
```

| Weg | Voraussetzung | Ergebnis |
|---|---|---|
| Direct Post | `TIKTOK_DIRECT_POST_ENABLED=true`, Scope `video.publish`, App von TikTok zugelassen | Video wird mit den gewählten Einstellungen veröffentlicht |
| Inbox-Upload | `TIKTOK_INBOX_UPLOAD_ENABLED=true`, Scope `video.upload` | Entwurf in der TikTok-Inbox, Veröffentlichung in der App |
| Manueller Export | immer (im Testmodus der einzige Weg) | ZIP mit `video.mp4`, `beschreibung.txt`, `untertitel.srt`, `HINWEISE.txt` |

* **OAuth:** Login Kit v2 mit PKCE (S256), `state` einmalig in Redis und an den angemeldeten Benutzer gebunden.
  Tokens verschlüsselt (Fernet), stündliche Erneuerung, Widerruf beim Trennen.
* **UX-Pflichten serverseitig erzwungen:** Sichtbarkeit ohne Voreinstellung und nur aus `privacy_level_options`;
  Kommentare/Duett/Stitch standardmäßig aus und bei im Konto deaktivierten Funktionen immer aus;
  kommerzielle Kennzeichnung (Your brand → `brand_organic_toggle`, Branded content → `brand_content_toggle`)
  mit Pflicht zur Auswahl; Branded Content nie mit `SELF_ONLY`; Einwilligungstext je nach Kennzeichnung;
  Videolänge ≤ `max_video_post_duration_sec`; zusätzlich Bestätigung der Streamer-Erlaubnis.
* **Kein Doppel-Post:** Freigabe nur einmal (`pending_review` → `approved`); der Worker setzt `approved` →
  `uploading` atomar (nur ein Worker gewinnt); nach Vergabe einer `publish_id` wird nie automatisch neu
  initialisiert. Fehlgeschlagene Aufträge gehen über *Erneut vorbereiten* zurück zur Prüfung.
* **Upload:** `FILE_UPLOAD` in Stücken (`TIKTOK_CHUNK_SIZE_MB`, Rest im letzten Stück, < 5 MB in einem Stück),
  `Content-Range` je Stück, Fortschritt im Dashboard. Danach `status/fetch` mit Backoff (15 s … 1 h, Abbruch nach 48 h).
* **Selbstheilung:** `tiktok_sync_publish` (10 min) nimmt verlorene Jobs nach Redis-/Worker-Neustart wieder auf.

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
* **Periodische Jobs** (`app/workers/scheduler.py`):

  | Job | Intervall | Zweck |
  |---|---|---|
  | `storage_cleanup` | 1 h | Videodateien nach Frist/Obergrenze löschen |
  | `prune_activity` | 24 h | Aktivitätsprotokoll kürzen |
  | `twitch_resolve` | 10 min | neue Streamer: Twitch-ID, Name, Profilbild, Sprache |
  | `twitch_refresh_known` | 24 h | umbenannte Logins/Profilbilder bekannter Kanäle |
  | `twitch_poll_live` | `TWITCH_POLL_INTERVAL_SECONDS` | Live-Status per Get Streams (Rückfallebene zu EventSub) |
  | `twitch_eventsub_sync` | 6 h | EventSub-Abos abgleichen (fehlende anlegen, defekte/überzählige löschen) |
  | `twitch_discover_clips` | `CLIP_DISCOVERY_INTERVAL_MINUTES` | öffentliche Clips suchen und bewerten |
  | `twitch_prune_clips` | 24 h | Clip-Metadaten nach `CLIP_RETENTION_DAYS` löschen |
  | `twitch_validate_tokens` | 1 h | Nutzer-Tokens bei Twitch validieren, erneuern oder sperren |
  | `clips_resume_stuck` | 30 min | Clip-Aufträge ohne laufenden Job (z. B. nach Redis-Neustart) wieder anstoßen |
  | `samples_ingest` | 5 min | nur `TEST_MODE=true`: neue Dateien aus `/data/samples` übernehmen |
  | `chat_flush` | 1 min | Chat-Zähler aus Redis in `chat_activity` übertragen (nur Zählwerte) |
  | `analysis_pending` | 10 min | Sicherheitsnetz: heruntergeladene, noch nicht analysierte Clips einreihen |

  Twitch-Jobs werden nur eingeplant, wenn Client-ID/Secret gesetzt sind und `TEST_MODE=false` ist.
  Ereignisgesteuert: nach `stream.online` Details nachladen (60 s), nach `stream.offline` VOD zuordnen (3 min,
  bis zu 4 Versuche) und Clips des Streams suchen (15 min); bei Modus B anschließend Kandidaten planen
  (15 min nach VOD-Zuordnung). Ab Phase 6 kommen TikTok-Statusabfragen hinzu.
* **Rate-Limits:** Die von Twitch gemeldeten Werte (`Ratelimit-Remaining`/`-Reset`) werden prozessübergreifend
  in Redis gehalten. Vor jeder Anfrage bleibt ein Puffer von 10 Punkten frei; reicht das Kontingent nicht,
  wird bis zu 15 s gewartet, sonst gibt der Job an RQ zurück. Bei HTTP 429 wird bis zum Reset gewartet,
  bei 401 einmal ein neues App-Token geholt, bei 5xx/Netzwerkfehlern zweimal mit Backoff wiederholt.
* **Live-Status ohne Flackern:** EventSub und Polling schreiben über dieselben idempotenten Funktionen.
  Ein beendeter Stream (gleiche Stream-ID) wird nie wieder geöffnet; direkt nach `stream.online` gilt eine
  Karenz von 5 Minuten, weil Get Streams neue Streams verzögert liefert.

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
├── migrations/                   ✓ Alembic (0001 Schema, 0002 Twitch, 0003 Autorisierung & Clips, 0004 KI-Analyse)
├── scripts/                      ✓ install_vps, init_env, backup, restore, healthcheck, update
├── docs/                         ✓ Architektur, API-Grenzen, Installation, Registrierung, Betrieb, Tests
├── tests/                        ✓ pytest (SQLite/PostgreSQL, fakeredis, später respx-Mocks)
└── app/
    ├── config.py, db.py, models.py, enums.py, timeutil.py, logging_setup.py, redis_client.py   ✓
    ├── cli.py                    ✓ Verwaltung (Benutzer, Streamer, Secrets, Bereinigung, Key-Rotation)
    ├── main.py                   ✓ FastAPI-App
    ├── security/                 ✓ crypto (Fernet), passwords (Argon2), csrf, ratelimit
    ├── services/                 ✓ activity, users, streamers (Rechte), permissions, storage, system,
    │                               ✓ twitch_monitor, eventsub_handler, clip_discovery, clip_scoring,
    │                               ✓ twitch_authorization, clip_processing, media_probe, samples, processing_keys,
    │                               ✓ highlight_analysis (Analyse, Duplikate, Auswahl), chat_stats, rendering
    ├── workers/                  ✓ queues, worker, scheduler, jobs/maintenance
    │   └── jobs/twitch.py        ✓  Streamer auflösen, Live-Status, Clip-Suche, EventSub-Abgleich, VOD-Zuordnung
    │   └── jobs/clips.py         ✓  Kandidaten planen, Clip aus VOD erzeugen, Download, Testmodus-Import
    │   └── jobs/analysis.py      ✓  KI-Analyse, Auswahl je Stream, Chat-Zähler übertragen
    │   └── jobs/render.py        ✓  Schnitt, Untertitel, Metadaten (automatisch nach der Auswahl)
    │   └── jobs/publish.py       6  TikTok-Upload und Statusverfolgung
    ├── integrations/twitch/      ✓  Helix-Client, App-Token, Nutzer-OAuth, EventSub, Rate-Limit
    ├── integrations/tiktok/      6  OAuth (PKCE), Creator-Info, Direct Post / Inbox-Upload, Status
    ├── ai/                       ✓  audio (FFmpeg/numpy), transcribe (faster-whisper), vision (OpenCV), scoring
    ├── media/                    ✓  layout (Facecam/Gameplay), subtitles (ASS/SRT), render (FFmpeg), metadata
    └── web/                      ✓ routes/, templates/, static/ (mobiloptimiert, ohne externe CDNs)
        └── routes/webhooks.py    ✓  EventSub-Endpunkt
        └── routes/oauth.py       ✓  Twitch-Einladung/Zustimmung/Callback (TikTok: Phase 6)
```

## 9. Empfohlene Servergröße

| Nutzung | vCPU | RAM | SSD |
|---|---|---|---|
| nur Modus A (Monitoring, Empfehlungen) | 2 | 2–4 GB | 20 GB |
| Modus B, Whisper `small` auf CPU, wenige Streamer | 4 | 8 GB (+4 GB Swap) | 80 GB |
| Modus B, viele Streamer / Whisper `medium` | 8 | 16 GB | 160 GB |

faster-whisper `small` mit `int8` benötigt auf CPU etwa 1–2 GB RAM und verarbeitet einen 45-s-Clip auf
4 vCPU in grob 10–30 s.
