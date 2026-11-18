#!/usr/bin/env bash
# Setzt einen Wert in der .env – funktioniert auf Linux und macOS gleich (anders als „sed -i“).
#
#   bash scripts/env_set.sh TEST_MODE true
#   bash scripts/env_set.sh TWITCH_CLIENT_ID abc123
#
# Existiert der Schlüssel noch nicht, wird er angehängt. Die Datei behält die Rechte 600.
set -euo pipefail
cd "$(dirname "$0")/.."

KEY="${1:-}"
VALUE="${2-}"
if [[ -z "${KEY}" || $# -lt 2 ]]; then
  echo "Aufruf: bash scripts/env_set.sh SCHLUESSEL WERT" >&2
  exit 1
fi
if [[ ! "${KEY}" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
  echo "Ungültiger Schlüssel: ${KEY}" >&2
  exit 1
fi
[[ -f .env ]] || { echo ".env fehlt – zuerst scripts/init_env.sh ausführen." >&2; exit 1; }

umask 077
TMP="$(mktemp .env.XXXXXX)"
trap 'rm -f "${TMP}"' EXIT
FOUND=false
while IFS= read -r line || [[ -n "${line}" ]]; do
  if [[ "${line}" == "${KEY}="* ]]; then
    printf '%s=%s\n' "${KEY}" "${VALUE}" >> "${TMP}"
    FOUND=true
  else
    printf '%s\n' "${line}" >> "${TMP}"
  fi
done < .env
[[ "${FOUND}" == "true" ]] || printf '%s=%s\n' "${KEY}" "${VALUE}" >> "${TMP}"
chmod 600 "${TMP}"
mv "${TMP}" .env
trap - EXIT
echo "✓ ${KEY} gesetzt"
