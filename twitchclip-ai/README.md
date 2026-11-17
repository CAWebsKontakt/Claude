# TwitchClip AI

Rechtekonformes System, das ausgewählte Twitch-Kanäle überwacht und – **nur mit dokumentierter Erlaubnis** –
aus Streams hochwertige vertikale TikTok-Clips erstellt. Läuft dauerhaft auf einem Ubuntu-24.04-VPS mit
Docker Compose, FastAPI, PostgreSQL, Redis/RQ, FFmpeg und faster-whisper.

## Zwei Betriebsarten

| | Modus A – öffentlich | Modus B – autorisiert |
|---|---|---|
| Voraussetzung | keine | dokumentierte Erlaubnis **und** Twitch-OAuth des Streamers/Editors |
| Live-Status (EventSub) | ✓ | ✓ |
| Öffentliche Clips: Links, Vorschau, Bewertung | ✓ | ✓ |
| Clip-Erstellung aus VOD, Download | – | ✓ (offizielle Helix-Endpunkte) |
| KI-Highlights, Schnitt 1080×1920, Untertitel | – | ✓ |
| TikTok (nach manueller Freigabe) | – | ✓, wenn die Erlaubnis Veröffentlichung umfasst |

## Schnellstart (VPS)

```bash
git clone https://github.com/CAWebsKontakt/Claude.git /opt/twitchclip && cd /opt/twitchclip/twitchclip-ai
sudo bash scripts/install_vps.sh                       # Docker, Firewall, Swap, Auto-Updates
bash scripts/init_env.sh clips.ihre-domain.de admin@ihre-domain.de
docker compose up -d --build
docker compose exec web python -m app.cli create-user --username admin --role admin
```

→ `https://clips.ihre-domain.de`

## Dokumentation

| Dokument | Inhalt |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architektur, Datenfluss, Datenmodell, Projektstruktur, Servergröße |
| [docs/API_CONSTRAINTS.md](docs/API_CONSTRAINTS.md) | Twitch- und TikTok-Beschränkungen und wie das System damit umgeht |
| [docs/INSTALL.md](docs/INSTALL.md) | Schritt-für-Schritt-Installation, Cron, Updates, Reboot-Test |
| [docs/API_REGISTRATION.md](docs/API_REGISTRATION.md) | Twitch- und TikTok-App registrieren, Review/Audit |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Streamer hinzufügen, Benutzer, Überwachung, Backup, Fehlerbehebung |
| [docs/TESTING.md](docs/TESTING.md) | Automatisierte Tests und Abnahmebefehle je Phase |

## Entwicklungsstand

| Phase | Inhalt | Status |
|---|---|---|
| 1 | Server, Docker, Datenbank, Dashboard, Benutzer, Rechte-Dokumentation, Speicherbereinigung | ✅ fertig |
| 2 | Twitch-API, EventSub, Live-Status, öffentliche Clip-Suche und -Bewertung | ✅ fertig |
| 3 | Twitch-OAuth für Streamer/Editoren, Clip aus VOD, Clip-Download, Idempotenz, Testmodus-Beispieldateien | ✅ fertig |
| 4 | KI-Highlight-Erkennung (Whisper, Audio, OpenCV, Chat, Duplikate, Score 0–100) | geplant |
| 5 | Vertikaler Schnitt, Facecam-Layout, Untertitel, Titel/Beschreibung/Hashtags | geplant |
| 6 | TikTok-OAuth, Creator-Info, Freigabe nach UX-Richtlinien, Upload, Status | geplant |
| 7 | Testmodus mit Beispieldateien, End-to-End-Tests, Produktionshärtung | geplant |

## Lizenz und Verantwortung

Die Software setzt technische Schranken (Modus A ohne Download, Rechteprüfung vor jedem Schritt, manuelle
Freigabe). Die rechtliche Verantwortung für eingeholte Erlaubnisse und veröffentlichte Inhalte bleibt beim
Betreiber.
