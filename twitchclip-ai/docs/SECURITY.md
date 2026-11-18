# Sicherheitsprüfung und Härtung (Phase 7)

Stand: Version 1.0.0. Geprüft wurden Code, Konfiguration und das laufende Docker-Setup (schreibgeschützte
Container, Caddy/HTTPS, Mock-APIs für Twitch und TikTok, Browser-Durchlauf mit Chromium).

## 1. Schutzziele

| Ziel | Bedeutung |
|---|---|
| Rechte Dritter | Kein Download, kein Schnitt, keine Veröffentlichung ohne dokumentierte Erlaubnis und OAuth – technisch erzwungen, nicht nur im UI |
| Konten | Twitch-/TikTok-Tokens dürfen nicht abfließen und nicht missbraucht werden |
| Keine ungewollten Posts | Nichts geht ohne einzelne, ausdrückliche Freigabe zu TikTok; kein Doppel-Post |
| Verfügbarkeit | Ausfälle werden erkannt und gemeldet; das System heilt sich nach Neustarts selbst |

## 2. Maßnahmen

### Anwendung
* **Rechte-Gate:** `compute_rights()` ist die einzige Quelle der Wahrheit; `ensure_can_process()` /
  `ensure_can_publish()` laufen unmittelbar vor Download, Analyse, Schnitt, Warteschlange, Freigabe **und** Upload.
  Ein Widerruf (Dashboard, Twitch-EventSub `user.authorization.revoke`, abgelehnte Token-Erneuerung, stündliche
  Validierung) stoppt alle folgenden Schritte – im E2E-Test nachgewiesen.
* **Online-Zustimmung:** Ein Zustimmungslink (signiert, einmalig, läuft ab) erzeugt nur dann eine Erlaubnis,
  wenn Twitch bestätigt, dass das angemeldete Konto der Kanalinhaber ist (Twitch-User-ID = Kanal-ID); sonst wird
  das Token sofort widerrufen. Angefragt werden kann nur, was der Link anbietet; die Auswahl liegt serverseitig im
  OAuth-State. Gespeichert werden Wortlaut (Fassung), Twitch-Konto, -ID und Zeitpunkt; eine neue Zustimmung
  ersetzt die alte.
* **Anmeldung:** Argon2id, Mindestlänge 12, Sperre nach Fehlversuchen je Benutzer und IP; Sitzungscookie
  `HttpOnly`, `Secure`, `SameSite=Lax`, max. 12 h, Widerruf aller Sitzungen über `session_version`.
* **Rollen:** Betrachter lesen nur; Redakteure verwalten Streamer/Highlights/Freigaben; nur Admins verwalten
  Benutzer und Betriebsaktionen. Jede schreibende Route prüft die Rolle serverseitig (Tests je Route).
* **CSRF:** Synchronizer-Token für jedes Formular; OAuth-`state` einmalig (Redis, 15 min) und an den
  angemeldeten Benutzer (TikTok) bzw. den signierten Einladungslink (Twitch) gebunden; TikTok zusätzlich PKCE (S256).
* **Header:** CSP ohne Inline-Skripte (`script-src 'self'`), `frame-ancestors 'none'`, `object-src 'none'`,
  `form-action` nur `'self'` plus die TikTok-Anmeldeseite (nötig, weil Browser die Regel auch auf die Weiterleitung
  nach „TikTok-Konto verbinden“ anwenden), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`,
  `Permissions-Policy`, `Cross-Origin-Opener-Policy`, HSTS (Caddy). API-Doku (`/docs`, `/openapi.json`) ist abgeschaltet.
* **Tokens:** Twitch- und TikTok-Tokens nur Fernet-verschlüsselt in der DB (Schlüsselrotation `rotate-token-key`),
  nie in Logs oder Templates; `SecretStr` für alle Secrets, Konfigurationsanzeige zeigt nur „gesetzt“.
* **Webhooks:** EventSub mit HMAC-SHA256 (konstante Laufzeit), Zeitstempel ≤ 10 min, Duplikatschutz per Message-ID.
* **Dateien:** Auslieferung nur nach Login und nur innerhalb von `/data` (Path-Traversal-Schutz);
  Downloads nur über HTTPS, mit Größenlimit, SHA-256 und ffprobe-Prüfung; FFmpeg ohne Shell (Argumentlisten).
* **Metriken:** `/metrics` ist ohne `METRICS_TOKEN` abgeschaltet (404) und verlangt sonst ein Bearer-Token
  (Vergleich in konstanter Laufzeit); enthält nur Zählwerte, keine Namen oder Inhalte.
* **Datenschutz:** Chat nur als anonyme Zählwerte (Redis, HyperLogLog); keine Chattexte gespeichert;
  Aktivitätsprotokoll mit Aufbewahrungsfrist.

### Container und Server
* Alle App-Container laufen als unprivilegierter Benutzer `app` (UID 10001), mit **schreibgeschütztem Dateisystem**
  (`read_only`), beschreibbar nur `/data` (Volume) und `/tmp` (tmpfs, 256 MB), **ohne Linux-Capabilities**
  (`cap_drop: ALL`; selbst `root` im Container kann keine fremden Dateien schreiben), `no-new-privileges`,
  Prozess-, Speicher- und CPU-Limits je Dienst.
* PostgreSQL und Redis nur im internen Netz `backend` (ohne Internet), Redis mit Passwort; Web-Port nur an `127.0.0.1`.
* Caddy: automatisches HTTPS, Admin-API aus, Request-Body ≤ 10 MB, `Server`-Header entfernt.
* VPS (`install_vps.sh`): UFW nur SSH/80/443 (443 auch UDP für HTTP/3), fail2ban, unattended-upgrades, Swap, Docker-Log-Rotation.

## 3. Ergebnisse der Prüfung (in Phase 7 behoben)

| # | Befund | Auswirkung | Behebung |
|---|---|---|---|
| 1 | Verzögerte Statusabfrage plante ihre Folgeabfrage mit derselben Job-ID; `enqueue_unique` verwarf sie, weil die laufende Abfrage diese ID noch belegte | TikTok-Status wurde erst nach bis zu 10 min (Sicherheitsabgleich) aktualisiert | Folgeabfragen mit laufender Nummer; Schutz gegen parallele Abfrageketten; Regressionstest im echten RQ-Worker |
| 2 | Nach Container-Neustart hielt der beendete Worker die RQ-Scheduler-Sperre; RQ versucht es erst nach 10 min erneut | verzögerte Jobs (Wiederholungen, Statusabfragen) lagen nach jedem Deploy bis ~20 min | Scheduler-Dienst reiht fällige verzögerte Jobs selbst ein und versucht die Sperre alle 5 s |
| 3 | CSP `form-action 'self'` blockiert im Browser die Weiterleitung nach „TikTok-Konto verbinden“ | Konto-Verbindung in Chrome nicht möglich | Origin der TikTok-Anmeldeseite in `form-action`, Test + Browser-E2E |
| 4 | `pids_limit` neben `deploy.resources.limits` macht die Compose-Datei ungültig | Start schlägt fehl | Limit als `deploy.resources.limits.pids` |
| 5 | Container-Dateisystem war beschreibbar (Code root-eigen, aber `/tmp`, `/etc` …) | größere Angriffsfläche | `read_only`, tmpfs `/tmp`, `cap_drop: ALL`, `HOME`/Caches nach `/tmp` bzw. `/data` |

## 4. Verbleibende Risiken und Empfehlungen

* **Nicht verifizierbare API-Details:** Die Pfade `TWITCH_VOD_CLIP_PATH`/`TWITCH_CLIP_DOWNLOAD_PATH` und einzelne
  TikTok-Fehlercodes konnten nicht gegen die echten APIs geprüft werden (Entwicklerportale aus der Testumgebung
  nicht erreichbar). Beides ist konfigurierbar bzw. wird mit Originalcode angezeigt. Erste echte Aufrufe im
  Sandbox-Modus von TikTok und mit einem eigenen Twitch-Kanal durchführen (`TESTING.md`, Phase 3 und 6).
* **Schlüssel:** `SECRET_KEY`, `TOKEN_ENCRYPTION_KEY`, DB-/Redis-Passwörter nur in `.env` (Rechte 600); bei
  Verdacht rotieren (`OPERATIONS.md`, Schlüssel rotieren). Backups enthalten verschlüsselte Tokens – Backups
  trotzdem geschützt aufbewahren, der Schlüssel liegt nicht im Backup.
* **Zwei-Faktor-Anmeldung** ist nicht eingebaut. Dashboard nur für wenige Personen freigeben; optional zusätzlich
  per Caddy `basic_auth` oder VPN vorschalten.
* **SSH:** Passwort-Login abschalten, nur Schlüssel (`PasswordAuthentication no` in `/etc/ssh/sshd_config`).
* **Updates:** `bash scripts/update.sh` regelmäßig (Basis-Images, Python-Pakete); Abhängigkeiten sind gepinnt.

## 5. Prüfbefehle

```bash
# Härtung der laufenden Container
for c in web worker worker-media scheduler; do
  docker inspect twitchclip-$c-1 --format "$c ro={{.HostConfig.ReadonlyRootfs}} caps={{.HostConfig.CapDrop}} pids={{.HostConfig.PidsLimit}} user={{.Config.User}}"
done
docker compose exec web sh -c 'touch /app/x; touch /tmp/ok && echo tmp-ok'      # /app: Read-only file system
# Sicherheits-Header
curl -sI https://clips.ihre-domain.de/login | grep -iE 'content-security|strict-transport|x-frame|referrer|permissions'
# Kein Zugriff ohne Login / ohne Token
curl -s -o /dev/null -w '%{http_code}\n' https://clips.ihre-domain.de/media/exports/1.zip   # 303 (Login)
curl -s -o /dev/null -w '%{http_code}\n' https://clips.ihre-domain.de/metrics               # 404 bzw. 401
# Offene Ports (nur 22, 80, 443 von außen)
sudo ss -tlpn | grep -vE '127.0.0.1|::1'
```
