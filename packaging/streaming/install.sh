#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

artifact_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -d -m 0750 -o radiotedu -g radiotedu /var/lib/radiotedu/fallback/en/assets
install -d -m 0750 -o radiotedu -g radiotedu /var/lib/radiotedu/fallback/fr/assets
install -d -m 0755 /opt/radiotedu-streaming
cp -R "$artifact_dir/liquidsoap" /opt/radiotedu-streaming/
cp "$artifact_dir/generate-playlist.sh" /opt/radiotedu-streaming/
chmod 0755 /opt/radiotedu-streaming/generate-playlist.sh

for station in en fr; do
  if [[ ! -f "/etc/radiotedu/fallback-${station}.env" ]]; then
    echo "missing /etc/radiotedu/fallback-${station}.env" >&2
    exit 1
  fi
  /opt/radiotedu-streaming/generate-playlist.sh "$station"
done

install -m 0644 "$artifact_dir/systemd/radiotedu-fallback-en.service" /etc/systemd/system/
install -m 0644 "$artifact_dir/systemd/radiotedu-fallback-fr.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now radiotedu-fallback-en.service radiotedu-fallback-fr.service
