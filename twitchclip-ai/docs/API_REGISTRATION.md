# Registrierung der API-Anwendungen

> Tipp: Die TikTok-Prüfung dauert erfahrungsgemäß Tage bis Wochen. Beantragen Sie sie früh – parallel zu den
> Phasen 2–5. Bis zur Freigabe funktionieren Inbox-Upload (sofern `video.upload` freigegeben) bzw. manueller Export.

## 1. Twitch (benötigt ab Phase 2)

1. Auf <https://dev.twitch.tv/console> mit einem Twitch-Konto anmelden. Für die Konsole ist
   **Zwei-Faktor-Authentifizierung** Pflicht.
2. **Anwendungen → Ihre Anwendung registrieren**
   * Name: eindeutig, z. B. `TwitchClip AI <IhrName>` (darf kein „Twitch“-Markenzeichen missbräuchlich nutzen).
   * **OAuth-Redirect-URL:** `https://clips.ihre-domain.de/oauth/twitch/callback` (exakt, mit https).
   * Kategorie: *Analytics Tool* oder *Application Integration*.
   * Client-Typ: **Vertraulich** (Confidential) – das Secret bleibt auf dem Server.
3. Nach dem Speichern **Verwalten** öffnen → **Client-ID** kopieren → **Neues Geheimnis** erzeugen und kopieren.
4. In der `.env` eintragen:

   ```ini
   TWITCH_CLIENT_ID=abc123...
   TWITCH_CLIENT_SECRET=xyz789...
   TWITCH_EVENTSUB_SECRET=<wurde von init_env.sh erzeugt>
   ```

5. `docker compose up -d` – die Dienste übernehmen die neue Konfiguration.

**Webhook-Voraussetzung:** `https://clips.ihre-domain.de/webhooks/twitch/eventsub` muss aus dem Internet über
Port 443 erreichbar sein (Caddy erledigt TLS automatisch).

**Was Streamer in Modus B tun:** Sie (oder ein Editor des Kanals) öffnen den Autorisierungslink aus dem
Dashboard, lesen die Erklärung der Berechtigungen, melden sich bei Twitch an und bestätigen die Scopes
`channel:manage:clips` bzw. `editor:manage:clips` (optional `user:read:chat`, `user:bot`, `channel:bot` für die
Chat-Statistik). Den Zugriff können sie jederzeit unter *Twitch → Einstellungen → Verbindungen* widerrufen –
TwitchClip AI erkennt das sofort (EventSub) und stoppt die Verarbeitung.

## 2. TikTok (benötigt ab Phase 6)

### 2.1 Vorbereitung

Für das App-Review verlangt TikTok öffentlich erreichbare Seiten:

* Datenschutzerklärung (z. B. `https://ihre-domain.de/datenschutz`)
* Nutzungsbedingungen (z. B. `https://ihre-domain.de/nutzungsbedingungen`)
* Ein Logo/Icon der App
* Ein kurzes **Demo-Video**, das den kompletten Ablauf zeigt (Login mit TikTok, Vorschau, Auswahl der
  Sichtbarkeit, Einwilligungshinweis, Freigabe, Statusanzeige). Das zeichnen Sie nach Phase 6 auf.

### 2.2 App anlegen

1. <https://developers.tiktok.com> → Konto anlegen/anmelden → **Manage apps → Connect an app**.
2. Typ: Web. App-Name, Beschreibung, Kategorie, Icon, Links zu Datenschutz/Nutzungsbedingungen ausfüllen.
3. **Products hinzufügen:**
   * **Login Kit** → Redirect-URI `https://clips.ihre-domain.de/oauth/tiktok/callback`
   * **Content Posting API** → „Direct Post“ aktivieren, falls später direkt veröffentlicht werden soll.
     Upload-Methode: **FILE_UPLOAD** (keine Domain-Verifizierung nötig).
4. **Scopes:** `user.info.basic`, `video.upload`, `video.publish`.
5. **Client Key** und **Client Secret** in die `.env`:

   ```ini
   TIKTOK_CLIENT_KEY=aw...
   TIKTOK_CLIENT_SECRET=...
   TIKTOK_DIRECT_POST_ENABLED=false
   ```

### 2.3 Review und Audit

| Stufe | Ergebnis |
|---|---|
| Sandbox / unauditiert | Tests mit wenigen Konten; Posts nur **privat** (`SELF_ONLY`), max. 5 Nutzer in 24 h |
| App-Review (Scopes freigegeben) | App darf die Scopes produktiv nutzen – Posts aber weiterhin nur privat |
| **Content-Posting-API-Audit** | öffentliche Posts erlaubt → dann `TIKTOK_DIRECT_POST_ENABLED=true` |

Für das Audit unter *Content Posting API → Apply for audit* den Antrag stellen und den Ablauf mit dem Demo-Video
belegen. TikTok prüft insbesondere die UX-Pflichten (siehe `API_CONSTRAINTS.md`, Abschnitt 2.5) – das
Freigabeformular von TwitchClip AI ist darauf ausgelegt.

## 3. Checkliste

- [ ] Twitch-App registriert, Redirect-URL exakt
- [ ] `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` in `.env`
- [ ] Domain erreichbar über HTTPS, `curl https://clips.ihre-domain.de/healthz` liefert `ok`
- [ ] TikTok-App angelegt, Login Kit + Content Posting API, Redirect-URI exakt
- [ ] Datenschutz- und Nutzungsbedingungen online
- [ ] `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` in `.env`
- [ ] App-Review beantragt, später Content-Posting-Audit
