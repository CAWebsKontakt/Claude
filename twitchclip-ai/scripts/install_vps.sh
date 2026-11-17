#!/usr/bin/env bash
# TwitchClip AI – Grundeinrichtung eines frischen Ubuntu-24.04-VPS.
# Idempotent: kann gefahrlos mehrfach ausgeführt werden.
#
#   sudo bash scripts/install_vps.sh [SWAP_GB]
#
# Installiert: Docker Engine + Compose-Plugin (offizielles Repo), UFW-Firewall,
# fail2ban, unattended-upgrades, Swap-Datei, Zeitzone Europe/Berlin.
set -euo pipefail

SWAP_GB="${1:-4}"
APP_USER="${APP_USER:-twitchclip}"

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen." >&2
  exit 1
fi

. /etc/os-release
if [[ "${ID}" != "ubuntu" ]]; then
  echo "Warnung: getestet für Ubuntu 24.04, gefunden: ${PRETTY_NAME}" >&2
fi

echo "==> Systempakete aktualisieren"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg git ufw fail2ban unattended-upgrades jq

echo "==> Zeitzone Europe/Berlin"
timedatectl set-timezone Europe/Berlin || true

echo "==> Docker installieren (offizielles Repository)"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker containerd

echo "==> Docker-Logrotation global begrenzen"
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" },
  "live-restore": true
}
JSON
  systemctl restart docker
fi

echo "==> Benutzer ${APP_USER} anlegen"
if ! id "${APP_USER}" >/dev/null 2>&1; then
  adduser --disabled-password --gecos "TwitchClip AI" "${APP_USER}"
fi
usermod -aG docker "${APP_USER}"
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 /opt/twitchclip

echo "==> Firewall (SSH, HTTP, HTTPS inkl. HTTP/3)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "==> fail2ban für SSH"
cat > /etc/fail2ban/jail.d/sshd.local <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo "==> Automatische Sicherheitsupdates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF

echo "==> Swap (${SWAP_GB} GB) – hilfreich für faster-whisper auf kleinen VPS"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-twitchclip.conf
fi

echo
echo "✓ Grundeinrichtung abgeschlossen."
echo "  Weiter als Benutzer ${APP_USER}:  sudo -iu ${APP_USER}"
echo "  Projekt klonen nach /opt/twitchclip und scripts/init_env.sh ausführen (siehe docs/INSTALL.md)."
