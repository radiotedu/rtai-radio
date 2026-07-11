#!/usr/bin/env bash
set -euo pipefail

station="$1"
asset_root="/var/lib/radiotedu/fallback/${station}/assets"
playlist="/var/lib/radiotedu/fallback/${station}/playlist.m3u"

if [[ "$station" != "en" && "$station" != "fr" ]]; then
  echo "station must be en or fr" >&2
  exit 2
fi

if [[ ! -d "$asset_root" ]]; then
  echo "missing fallback asset directory: $asset_root" >&2
  exit 1
fi

tmp_playlist="${playlist}.tmp"
find "$asset_root" -type f \( -iname '*.mp3' -o -iname '*.ogg' -o -iname '*.flac' -o -iname '*.wav' \) -print | sort > "$tmp_playlist"
if [[ ! -s "$tmp_playlist" ]]; then
  rm -f "$tmp_playlist"
  echo "refusing to install an empty fallback playlist for $station" >&2
  exit 1
fi
mv "$tmp_playlist" "$playlist"
