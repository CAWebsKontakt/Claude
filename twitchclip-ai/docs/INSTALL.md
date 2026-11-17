# Installation auf einem Ubuntu-24.04-VPS

Ziel: TwitchClip AI läuft dauerhaft unter `https://clips.ihre-domain.de`, startet nach einem Reboot selbst und
sichert die Datenbank täglich.

## 0. Voraussetzungen

* VPS mit Ubuntu 24.04 LTS, mind. 2 vCPU / 4 GB RAM (für KI-Verarbeitung ab Phase 4: 4 vCPU / 8 GB, 80 GB SSD).
* Eine (Sub-)Domain, deren **A-Record** (und ggf. AAAA) auf die öffentliche IP des VPS zeigt.
  Prüfen: `dig +short clips.ihre-domain.de` muss die Server-IP liefern.
* SSH-Zugang als `root` oder Benutzer mit `sudo`.
* Ports 80 und 443 dürfen beim Hoster nicht gesperrt sein (Let's Encrypt + Twitch-Webhooks).

## 1. Server vorbereiten (einmalig, als root)

```bash
ssh root@IHRE-SERVER-IP

# Repository holen (privates Repo: vorher Deploy-Key oder Personal Access Token einrichten)
apt-get update && apt-get install -y git
git clone https://github.com/CAWebsKontakt/Claude.git /opt/twitchclip
cd /opt/twitchclip/twitchclip-ai

# Docker, Firewall (22/80/443), fail2ban, Auto-Updates, 4 GB Swap, Zeitzone
sudo bash scripts/install_vps.sh 4

# Dateien dem Dienstbenutzer übergeben
chown -R twitchclip:twitchclip /opt/twitchclip
```

> Läuft SSH auf einem anderen Port als 22, vor dem Skript `ufw allow <PORT>/tcp` ausführen.
> Hinweis: Von Docker veröffentlichte Ports umgehen UFW. Deshalb ist der Web-Port nur an `127.0.0.1`
> gebunden und PostgreSQL/Redis sind gar nicht veröffentlicht.

## 2. Konfiguration

```bash
sudo -iu twitchclip
cd /opt/twitchclip/twitchclip-ai

# .env mit frischen Zufallswerten erzeugen (Domain + E-Mail für Let's Encrypt)
bash scripts/init_env.sh clips.ihre-domain.de admin@ihre-domain.de

# Fachliche Werte anpassen (STREAMER_LIST, MAX_CLIPS_PER_DAY, MIN_HIGHLIGHT_SCORE, …)
nano .env
```

**Wichtig:** Den Wert `TOKEN_ENCRYPTION_KEY` zusätzlich in einem Passwortmanager sichern. Ohne ihn sind gespeicherte
OAuth-Tokens nach einer Wiederherstellung unbrauchbar (Streamer müssten neu autorisieren).

## 3. Starten

```bash
docker compose up -d --build
docker compose ps
```

Erwartet: `postgres`, `redis` = healthy; `migrate` = exited (0); `web` = healthy; `worker`, `worker-media`,
`scheduler`, `caddy` = running. Der erste Start dauert einige Minuten (Image-Build, Zertifikat).

```bash
# Ersten Administrator anlegen (Passwort wird verdeckt abgefragt, mind. 12 Zeichen)
docker compose exec web python -m app.cli create-user --username admin --role admin
```

Dashboard öffnen: `https://clips.ihre-domain.de` → anmelden.

## 4. Regelmäßige Aufgaben (Cron als Benutzer `twitchclip`)

```bash
crontab -e
```

```cron
# Datenbank-Backup täglich 03:15, 14 Tage Aufbewahrung
15 3 * * * cd /opt/twitchclip/twitchclip-ai && bash scripts/backup.sh >> backups/backup.log 2>&1
# Gesundheitsprüfung alle 5 Minuten (Alarm per ntfy/Discord/Slack-Webhook optional)
*/5 * * * * cd /opt/twitchclip/twitchclip-ai && ALERT_WEBHOOK_URL=https://ntfy.sh/IHR-GEHEIMES-TOPIC bash scripts/healthcheck.sh >> backups/health.log 2>&1
```

Backups zusätzlich regelmäßig vom Server herunterkopieren (z. B. `rsync`/`restic` auf einen anderen Speicher).

## 5. Reboot-Test

```bash
sudo reboot
# nach ca. 1 Minute erneut verbinden:
cd /opt/twitchclip/twitchclip-ai && docker compose ps
curl -fsS http://127.0.0.1:8000/readyz
```

Alle Dienste müssen wieder laufen (`restart: unless-stopped`, Docker per systemd aktiviert).

## 6. Updates

```bash
cd /opt/twitchclip/twitchclip-ai
bash scripts/update.sh      # Backup → git pull → Build → Neustart (Migrationen laufen automatisch)
```

## 7. Zugriff ohne Domain (nur zum Testen)

Solange DNS noch nicht steht, lässt sich das Dashboard per SSH-Tunnel aufrufen:

```bash
# auf Ihrem Rechner
ssh -L 8000:127.0.0.1:8000 twitchclip@IHRE-SERVER-IP
# Browser: http://localhost:8000  (Browser behandeln localhost als sicheren Kontext, Login funktioniert)
```

Caddy versucht in dieser Zeit erfolglos ein Zertifikat zu bekommen; das ist unkritisch.

## 8. Deinstallation

```bash
cd /opt/twitchclip/twitchclip-ai
docker compose down            # Container stoppen, Daten bleiben
docker compose down -v         # ACHTUNG: löscht auch Datenbank, Redis und Videos
```
