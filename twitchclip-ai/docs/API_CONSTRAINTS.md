# Twitch- und TikTok-API: Beschränkungen und Konsequenzen

> Stand: November 2026. Plattform-APIs ändern sich. Werte, die hier als **„prüfen“** markiert sind, konnten nicht
> direkt gegen die offizielle Referenz verifiziert werden und sind im Code konfigurierbar bzw. werden beim ersten
> echten Aufruf protokolliert. Vor dem Produktivstart bitte mit der aktuellen Dokumentation abgleichen:
> <https://dev.twitch.tv/docs/api/reference/> und <https://developers.tiktok.com/doc/content-posting-api-get-started>.

## Teil 1 – Twitch

### 1.1 Grundsätzliches zu Rechten

* Inhalte auf Twitch gehören den Streamern (bzw. Dritten wie Musik- oder Spielepublishern). Die API-Nutzung
  gibt **keine** Rechte zur Weiterverwendung von Videos.
* Für öffentliche Kanäle dürfen wir über die API **Metadaten** abrufen und Clips über den **offiziellen
  Embed-Player** oder per Link anzeigen. Herunterladen, Umschneiden und Neuveröffentlichen ohne Erlaubnis ist
  nicht zulässig → Modus A.
* Inoffizielle Wege (z. B. MP4-Adresse aus der Thumbnail-URL ableiten, interne GraphQL-API der Website,
  `youtube-dl`/`streamlink` für VODs) werden **nicht** verwendet – auch nicht in Modus B.

### 1.2 Authentifizierung

| Token | Woher | Wofür |
|---|---|---|
| App-Access-Token (Client Credentials) | `POST https://id.twitch.tv/oauth2/token` mit Client-ID/Secret | Get Users, Get Streams, Get Clips, Get Videos, EventSub-Webhooks |
| User-Access-Token (Authorization Code) | Streamer/Editor meldet sich an und stimmt Scopes zu | Create Clip From VOD, Get Clips Download, Chat (EventSub), Stream-Marker |

User-Tokens laufen ab (typisch wenige Stunden) und werden mit dem Refresh-Token erneuert. Ändert der Nutzer sein
Passwort oder trennt er die App, werden Tokens ungültig → Status „Twitch-Autorisierung fehlt“, Verarbeitung stoppt.
Twitch verlangt außerdem, dass Apps User-Tokens regelmäßig über `GET /oauth2/validate` prüfen (ab Phase 3 stündlich).

### 1.3 Rate-Limits (Helix)

* Token-Bucket pro Client-ID (App-Token) bzw. pro Client-ID + Nutzer (User-Token); Standard **800 Punkte pro
  Minute**, die meisten Endpunkte kosten 1 Punkt.
* Antwort-Header: `Ratelimit-Limit`, `Ratelimit-Remaining`, `Ratelimit-Reset` (Unix-Zeit). Bei Überschreitung
  HTTP **429**.
* Umsetzung: zentraler Redis-Token-Bucket je Token, Warten bis `Ratelimit-Reset` bei 429, Batch-Abfragen
  (Get Streams/Get Users mit bis zu **100** IDs/Logins pro Anfrage).
* Einige neue Endpunkte haben eigene Limits, z. B. **Get Clips Download: 100 Anfragen/Minute**.

### 1.4 Live-Erkennung: EventSub

* Abos `stream.online` und `stream.offline` (Version 1, Bedingung `broadcaster_user_id`) benötigen **keine**
  Autorisierung des Streamers → auch für Modus A nutzbar.
* Transport **Webhook**: öffentlich erreichbare HTTPS-URL auf **Port 443** mit gültigem Zertifikat (Caddy),
  App-Access-Token zum Anlegen.
* Jede Nachricht ist per **HMAC-SHA256** signiert: `HMAC(secret, Message-Id + Timestamp + Body)`, Header
  `Twitch-Eventsub-Message-Id`, `-Timestamp`, `-Signature` (`sha256=…`), `-Message-Type`
  (`webhook_callback_verification`, `notification`, `revocation`). Nachrichten älter als 10 Minuten verwerfen,
  Duplikate über die Message-ID erkennen, schnell mit 2xx antworten (sonst Wiederholungen und Widerruf).
* **Kosten-Limit:** Ein Abo kostet 1, wenn der Streamer die App nicht autorisiert hat, sonst 0. Pro Client-ID
  stehen standardmäßig **10.000** Kostenpunkte für Webhooks zur Verfügung (`max_total_cost`) → bei 2 Abos je
  Kanal ca. 5.000 öffentliche Kanäle. Mehr als genug für dieses Projekt.
* Rückfallebene: alle 3–5 Minuten `Get Streams` für alle aktiven Kanäle (100 pro Anfrage), falls ein Webhook
  verloren geht oder das Abo widerrufen wurde.
* WebSocket-Transport ist für Server mit vielen fremden Kanälen ungeeignet (User-Token nötig, kleines Kostenlimit).

### 1.5 Öffentliche Clips (Modus A): Get Clips

* `GET /helix/clips?broadcaster_id=…&started_at=…&ended_at=…&first=100` (Paginierung per Cursor).
* Felder u. a.: `id`, `url`, `embed_url`, `title`, `view_count`, `created_at`, `duration`, `thumbnail_url`,
  `creator_name`, `video_id`, `vod_offset`, `is_featured`, `game_id`, `language`.
* **Keine Download-URL** im Ergebnis (von Twitch bewusst so entschieden).
* Lange Zeiträume liefern nicht zuverlässig alle Clips → in kurzen Fenstern (z. B. 6–24 h) abfragen.
* `vod_offset` kann `null` sein (VOD gelöscht oder noch in Verarbeitung) und ist ein guter Hinweis auf
  „Hotspots“ eines Streams.
* Einbettung: `https://clips.twitch.tv/embed?clip=<id>&parent=<ihre-domain>` – `parent` ist Pflicht und muss
  exakt der Dashboard-Domain entsprechen; Einbettung funktioniert nur über HTTPS.

### 1.6 Autorisierte Verarbeitung (Modus B)

| Endpunkt | Zweck | Voraussetzung |
|---|---|---|
| **Create Clip From VOD** (`POST`, Pfad „prüfen“, Parameter `editor_id`, `broadcaster_id`, `vod_id`, `vod_offset`, `duration`, `title`) | Clip aus einem VOD-Zeitpunkt erzeugen – auch aus dem VOD eines laufenden Streams | User-Token des Broadcasters oder eines Channel-Editors mit `editor:manage:clips` oder `channel:manage:clips` |
| **Get Clips Download** (`GET /helix/clips/downloads`, „prüfen“: Pfad) | temporäre Download-URLs (`landscape_download_url`, `portrait_download_url`) für bis zu **10** Clips pro Anfrage | dieselben Scopes; `editor_id` muss die App autorisiert haben; 100 Anfragen/Minute |

Wichtige Details und Grenzen:

* `vod_offset` bezeichnet das **Ende** des Clips; der Clip beginnt bei `vod_offset − duration`.
  `duration` ist laut Bibliotheken/Forum standardmäßig 30 s; erlaubt sind ca. **5–60 s** („prüfen“).
  → `CLIP_DURATION_MAX` ist auf 60 begrenzt.
* Ein VOD muss existieren: Der Streamer muss „Vergangene Übertragungen speichern“ aktiviert haben. VODs werden
  je nach Status nur begrenzt aufbewahrt (üblicherweise **7 Tage**, für Partner/Turbo/Prime länger) – die
  Verarbeitung muss also zeitnah nach dem Stream erfolgen.
* Gemutete VOD-Abschnitte (DMCA-Musik) bleiben stumm.
* Download-URLs sind kurzlebig → sofort herunterladen, nie speichern.
* Laut Entwicklerforum liefert der Download teils nicht die Quellauflösung (z. B. 1080p H.264 statt 1440p HEVC);
  für Hochformat genügt das.
* Per API erzeugte Clips sind im Clip-Manager des Kanals sichtbar. Darum erzeugen wir nur wenige, gezielt
  ausgewählte Clips je Stream (Kandidatenlimit) und benennen sie nachvollziehbar.
* **Es gibt keine offizielle API zum Herunterladen kompletter VODs.** Daher der kandidatenbasierte Ansatz
  (siehe `ARCHITECTURE.md`).

### 1.7 Chat

* Offiziell: EventSub `channel.chat.message`. Benötigt `user:read:chat` (Bot-Konto) und für den Webhook-Transport
  zusätzlich `user:bot` (Bot) sowie `channel:bot` (Broadcaster) bzw. Moderatorstatus des Bots.
* TwitchClip AI nutzt Chat-Daten nur in Modus B und speichert **nur Zählwerte** (Nachrichten, verschiedene
  Absender, Emotes, Hype-Schlüsselwörter je 10 s) – keine Nachrichtentexte, keine Benutzernamen (DSGVO,
  Datensparsamkeit).
* Eine Chat-Wiedergabe für alte VODs gibt es in der offiziellen API nicht → Chat wird live mitgezählt.

## Teil 2 – TikTok

### 2.1 Produkte und Scopes

| Produkt/Scope | Wofür |
|---|---|
| Login Kit, `user.info.basic` | OAuth-Anmeldung, Anzeigename/Avatar |
| Content Posting API, `video.upload` | Video als **Entwurf in die TikTok-Inbox** laden; der Nutzer veröffentlicht in der TikTok-App selbst |
| Content Posting API, `video.publish` | **Direct Post** – direkt veröffentlichen |

Scopes müssen im Developer-Portal beantragt und im App-Review freigegeben werden.

### 2.2 Unauditierte Apps (Ausgangszustand!)

* Inhalte von nicht auditierten Clients sind **nur privat** sichtbar (`SELF_ONLY`); öffentliche Posts werden
  bei der Initialisierung abgelehnt (`unaudited_client_can_only_post_to_private_accounts`).
* Höchstens **5 Nutzer** dürfen innerhalb von 24 h über eine unauditierte App posten.
* Die Freischaltung öffentlicher Posts erfordert das **Content-Posting-API-Audit** (separat vom normalen
  App-Review). TikTok prüft dabei u. a. die Umsetzung der UX-Richtlinien (siehe 2.5).
* Konsequenz im Code: `TIKTOK_DIRECT_POST_ENABLED=false` (Standard) → Direct Post ist ausgeblendet; angeboten
  werden **Inbox-Upload** (falls `video.upload` freigegeben) oder **manueller Export** (MP4 + Text + Hashtags zum
  Herunterladen). Wir setzen niemals öffentliche Sichtbarkeit oder Rechte voraus, die nicht vorhanden sind.

### 2.3 OAuth

* Autorisierung: `https://www.tiktok.com/v2/auth/authorize/` (`client_key`, `scope`, `response_type=code`,
  `redirect_uri`, `state`; PKCE `code_challenge` wird mitgesendet).
* Token: `POST https://open.tiktokapis.com/v2/oauth/token/`. Access-Token gilt **24 h**, Refresh-Token
  **365 Tage**; Widerruf: `POST /v2/oauth/revoke/`.
* `redirect_uri` muss exakt im Portal registriert sein (HTTPS).
* Tokens werden verschlüsselt gespeichert und automatisch erneuert; ein fehlgeschlagener Refresh setzt das
  Konto auf „neu verbinden“.

### 2.4 Ablauf eines Posts

1. **Creator-Info abfragen** (`POST /v2/post/publish/creator_info/query/`) – Pflicht direkt vor jeder
   Veröffentlichung: `creator_nickname`, `creator_avatar_url`, `privacy_level_options`, `comment_disabled`,
   `duet_disabled`, `stitch_disabled`, `max_video_post_duration_sec`. Kann der Creator gerade nicht posten
   (Tageslimit), wird das hier bzw. bei der Initialisierung gemeldet.
2. **Initialisieren**
   * Direct Post: `POST /v2/post/publish/video/init/` mit `post_info` (`title` = Bildunterschrift inkl.
     Hashtags, max. 2.200 Zeichen; `privacy_level`; `disable_comment`/`disable_duet`/`disable_stitch`;
     `video_cover_timestamp_ms`; `brand_content_toggle`; `brand_organic_toggle`; `is_aigc`) und
     `source_info` (`source=FILE_UPLOAD`, `video_size`, `chunk_size`, `total_chunk_count`).
   * Inbox-Upload: `POST /v2/post/publish/inbox/video/init/` nur mit `source_info`.
   * Antwort: `publish_id` und `upload_url` (ca. 1 h gültig).
   * `PULL_FROM_URL` würde eine verifizierte Domain erfordern – wir nutzen `FILE_UPLOAD`.
3. **Hochladen**: `PUT upload_url` mit `Content-Type: video/mp4` und `Content-Range: bytes start-end/total`,
   Chunks nacheinander. Chunk-Regeln: mind. 5 MB, max. 64 MB (letzter Chunk größer erlaubt, „prüfen“: bis
   128 MB), Videos < 5 MB in einem Stück, `total_chunk_count = floor(video_size / chunk_size)`, max. 1.000 Chunks.
   Unsere 30–45-s-Clips (ca. 10–40 MB) passen meist in 1–2 Chunks.
4. **Status verfolgen**: `POST /v2/post/publish/status/fetch/` → `PROCESSING_UPLOAD`, `PROCESSING_DOWNLOAD`,
   `SEND_TO_USER_INBOX`, `PUBLISH_COMPLETE`, `FAILED` (+ `fail_reason`, ggf. Post-ID).

### 2.5 Pflichten aus den TikTok-UX-Richtlinien (Direct Post)

Werden in Phase 6 im Freigabeformular umgesetzt:

* Creator-Avatar und -Nickname anzeigen, damit klar ist, in welches Konto gepostet wird.
* Vorschau des Videos; Titel/Beschreibung durch den Nutzer editierbar.
* **Sichtbarkeit muss der Nutzer selbst wählen** – nur Werte aus `privacy_level_options`, **kein
  voreingestellter Wert** (im Modell daher `privacy_level = NULL` bis zur Auswahl).
* Kommentare/Duett/Stitch als Schalter, die bei `*_disabled` gesperrt sind; keine stillschweigenden Vorgaben.
* Kennzeichnung kommerzieller Inhalte: Schalter standardmäßig aus; bei „Ihre Marke“ bzw. „Markeninhalt“ die
  jeweils vorgeschriebene Einwilligungserklärung; Markeninhalt darf nicht privat sein.
* Einwilligungshinweis vor dem Posten (u. a. „Music Usage Confirmation“, bei Markeninhalt zusätzlich „Branded
  Content Policy“) mit den von TikTok vorgegebenen Formulierungen.
* Videolänge gegen `max_video_post_duration_sec` prüfen.
* Keine Wasserzeichen/Werbebanner der App ins Video einbrennen.
* Nach dem Absenden darauf hinweisen, dass die Verarbeitung einige Minuten dauern kann.
* Keine automatischen Posts: Jede Veröffentlichung wird einzeln vom Nutzer ausgelöst.

### 2.6 Limits

* Init (Direct Post/Inbox): laut Sekundärquellen **6 Anfragen/Minute** je User-Token („prüfen“).
* Creator-Info und Status: eigene, großzügigere Limits; bei 429 (`rate_limit_exceeded`) mit Backoff wiederholen.
* Postinglimit je Creator: etwa **15 Posts pro 24 h**, geteilt über alle API-Clients (variiert; Fehler
  `spam_risk_too_many_posts`). Zusammen mit `MAX_CLIPS_PER_DAY` und der manuellen Freigabe unkritisch.
* Video: MP4 (H.264 empfohlen) + AAC, max. 4 GB, 23–60 fps, mind. 360 px – unser Rendering (1080×1920, H.264,
  AAC, 30 fps) erfüllt das.

### 2.7 Musik und Urheberrecht

* TwitchClip AI fügt **keine** Musik hinzu.
* Im Stream-Ton kann dennoch fremde Musik enthalten sein (Hintergrundmusik, Spielsoundtrack). Das ist durch die
  Erlaubnis des Streamers nicht automatisch abgedeckt. Die Rechte-Dokumentation hat dafür ein Feld „Umfang /
  Einschränkungen“; die Freigabe erfolgt bewusst manuell.
* TikToks „Music Usage Confirmation“ ist eine Erklärung des Nutzers, keine Lizenz.

## Teil 3 – Was daraus für das System folgt

| Einschränkung | Umsetzung |
|---|---|
| Kein Recht an fremden Inhalten | Modus A = nur Metadaten/Links/Embeds; Modus B nur mit dokumentierter Erlaubnis + OAuth |
| Kein VOD-Download | Kandidaten → Create Clip From VOD → Get Clips Download |
| VOD-Aufbewahrung begrenzt | Verarbeitung direkt nach `stream.offline`; Warnung, wenn kein VOD vorhanden |
| Clip-Länge 5–60 s | `CLIP_DURATION_MIN/MAX` validiert (max. 60), Standard 20–45 |
| Helix 800 Punkte/min | Redis-Token-Bucket, Batch-Abfragen, Backoff bis `Ratelimit-Reset` |
| EventSub braucht HTTPS/443 | Caddy mit Let's Encrypt; Polling als Rückfallebene |
| TikTok unauditiert = privat, ≤ 5 Nutzer | Direct Post standardmäßig aus; Inbox-Upload oder manueller Export |
| TikTok-UX-Pflichten | Freigabeformular ohne Voreinstellungen, Creator-Info direkt vor dem Post |
| TikTok-Tageslimit | `MAX_CLIPS_PER_DAY`, manuelle Freigabe, Fehler werden im Dashboard angezeigt |
