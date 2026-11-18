# TwitchClip AI auf dem Mac starten (ohne Server, ohne Domain)

Läuft auf jedem Mac mit Apple-Chip (M1–M4) oder Intel. Dauer: etwa 20–30 Minuten, davon das meiste Warten.

**Was lokal geht:** Dashboard, Selbsttest, Live-Erkennung, öffentliche Clips, Hype-Liste, Streamer beobachten,
dein eigener Kanal (Modus B mit Schnitt), manueller TikTok-Export.
**Was eine öffentliche Adresse braucht:** Zustimmungslinks für *andere* Streamer (ihr Browser muss deinen Mac
erreichen) – dafür gibt es unten in Schritt 7 einen kostenlosen Tunnel oder später einen Server.

## 1. Voraussetzungen

* macOS 13 oder neuer, mindestens 8 GB RAM (16 GB empfohlen), 20 GB freier Speicher
* **Docker Desktop** (für private Nutzung kostenlos): <https://www.docker.com/products/docker-desktop/> →
  „Download for Mac – Apple Chip“ bzw. „Intel Chip“ → installieren → öffnen → Lizenz bestätigen.
* In Docker Desktop: ⚙️ *Settings* →
  * *General* → **Start Docker Desktop when you sign in** einschalten
  * *Resources* → **Memory mindestens 6 GB** (bei 8-GB-Macs 5 GB, siehe Tipp in Schritt 9)

## 2. Herunterladen und einrichten

Programm **Terminal** öffnen (Spotlight: ⌘ + Leertaste → „Terminal“) und nacheinander einfügen:

```bash
cd ~
git clone -b claude/twitchclip-ai-system-4or03e https://github.com/CAWebsKontakt/Claude.git twitchclip
cd ~/twitchclip/twitchclip-ai
bash scripts/init_env.sh --local
```

* Fragt der Mac beim ersten `git` nach den „Befehlszeilenentwickler-Tools“: **Installieren** wählen, warten,
  dann den Befehl wiederholen.
* Fragt `git` nach einem Login (privates Repository): mit deinem GitHub-Konto anmelden oder auf GitHub
  den Branch `claude/twitchclip-ai-system-4or03e` auswählen → **Code → Download ZIP**, entpacken nach
  `~/twitchclip` und dort weitermachen.

## 3. Starten

```bash
cd ~/twitchclip/twitchclip-ai
docker compose up -d --build          # beim ersten Mal 5–15 Minuten (lädt und baut alles)
docker compose ps                     # alle Dienste „running“, web „healthy“
docker compose exec web python -m app.cli create-user --username admin --role admin
open http://localhost:8000
```

Passwort mindestens 12 Zeichen. Dann im Browser anmelden – fertig.

## 4. Selbsttest (prüft die ganze Kette mit Beispielvideos)

```bash
bash scripts/env_set.sh TEST_MODE true && docker compose up -d
docker compose exec worker-media python -m app.cli selftest --no-transcribe   # endet mit „SELBSTTEST BESTANDEN“
bash scripts/env_set.sh TEST_MODE false && docker compose up -d
```

Ohne `--no-transcribe` wird zusätzlich die Spracherkennung getestet (lädt beim ersten Mal ein Modell, ~500 MB).
Im Dashboard unter *Highlights* siehst du danach die geschnittenen Testvideos.

## 5. Twitch verbinden

1. <https://dev.twitch.tv/console/apps> → mit deinem Twitch-Konto anmelden (Zwei-Faktor-Anmeldung muss an
   sein) → **Register Your Application**
   * Name: z. B. `TwitchClip Mac` (muss bei Twitch einmalig sein)
   * OAuth Redirect URLs: `http://localhost:8000/oauth/twitch/callback`
   * Category: *Application Integration* · Client Type: *Confidential*
2. **Manage** → **Client-ID** kopieren, **New Secret** → Secret kopieren, dann im Terminal:

```bash
cd ~/twitchclip/twitchclip-ai
bash scripts/env_set.sh TWITCH_CLIENT_ID hier-die-client-id
bash scripts/env_set.sh TWITCH_CLIENT_SECRET hier-das-secret
docker compose up -d
docker compose exec worker python -m app.cli check-config
```

Danach füllt sich die **Hype-Liste** innerhalb von 15 Minuten; das Wachstum („+120 % in 60 min“) erscheint nach
etwa einer Stunde. Streamer zum Beobachten: Dashboard → *Streamer* → Name eintragen.

## 6. Mac wach halten

Das System arbeitet nur, solange der Mac an ist und nicht schläft:

* *Systemeinstellungen → Batterie → Optionen* (MacBook) bzw. *Energie* (iMac/Mac mini):
  **„Automatischen Ruhezustand verhindern, wenn das Display aus ist“** (am Netzteil) einschalten, **oder**
* solange es laufen soll, im Terminal: `caffeinate -i` (Fenster offen lassen, beenden mit Ctrl + C).

Nach einem Neustart des Macs startet Docker Desktop automatisch und mit ihm TwitchClip AI.

## 7. Andere Streamer um Zustimmung bitten (öffentliche Adresse)

Ein Zustimmungslink mit `http://localhost:8000` funktioniert nur auf deinem Mac. Damit Streamer ihn öffnen
können, braucht dein Mac vorübergehend eine öffentliche HTTPS-Adresse – kostenlos mit Cloudflare:

```bash
brew install cloudflared            # Homebrew: https://brew.sh (einmalig)
cloudflared tunnel --url http://localhost:8000
```

Die Ausgabe zeigt eine Adresse wie `https://abc-def-ghi.trycloudflare.com`. Dann (Terminal-Fenster offen lassen,
in einem zweiten Fenster):

```bash
cd ~/twitchclip/twitchclip-ai
bash scripts/env_set.sh APP_BASE_URL https://abc-def-ghi.trycloudflare.com
docker compose up -d
```

und in der Twitch-Konsole bei *OAuth Redirect URLs* zusätzlich `https://abc-def-ghi.trycloudflare.com/oauth/twitch/callback`
eintragen. Jetzt erzeugte Zustimmungslinks funktionieren überall.

Wichtig: Die Adresse ändert sich bei jedem Start von `cloudflared` – Links mit der alten Adresse gehen dann nicht
mehr. Während der Tunnel läuft, ist dein Dashboard aus dem Internet erreichbar (Login nötig, starkes Passwort!).
Zurück zu lokal: `bash scripts/env_set.sh APP_BASE_URL http://localhost:8000 && docker compose up -d`.
Für den Dauerbetrieb mit vielen Streamern ist ein kleiner Server die bessere Wahl (`docs/INSTALL.md`).

## 8. Alltag

| Was | Befehl (im Ordner `~/twitchclip/twitchclip-ai`) |
|---|---|
| Status | `docker compose ps` |
| Anhalten / Weiter | `docker compose stop` / `docker compose start` |
| Logs | `docker compose logs -f --tail=50 web worker worker-media` |
| Probleme anzeigen | `docker compose exec worker python -m app.cli check-alerts` |
| Update | `git pull && docker compose up -d --build` |
| Sicherung | `bash scripts/backup.sh` (Ordner `backups/`) |

## 9. Fehlerbehebung

| Meldung | Lösung |
|---|---|
| `Cannot connect to the Docker daemon` | Docker Desktop öffnen und warten, bis der Wal oben in der Menüleiste ruhig ist |
| `port is already allocated` (8000) | anderes Programm nutzt Port 8000 – beenden oder Mac neu starten |
| Worker-Media startet neu / „Killed“ | Docker Desktop mehr Speicher geben, oder bei 8-GB-Macs: `bash scripts/env_set.sh MEDIA_WORKER_MEMORY 2g` und `bash scripts/env_set.sh WHISPER_MODEL base`, dann `docker compose up -d` |
| Login klappt nicht | `docker compose exec web python -m app.cli reset-password --username admin` |
| Schnitt sehr langsam | `bash scripts/env_set.sh RENDER_PRESET veryfast && docker compose up -d` |
