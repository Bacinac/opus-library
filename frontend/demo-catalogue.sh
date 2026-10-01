#!/usr/bin/env bash
# Place the demo catalogue — the fixture, its pictures and the credits page —
# into a built demo. Library owns the catalogue; the Player demo shows the same
# household through its own screens and takes it from here.
#
#   ./demo-catalogue.sh <out-dir>
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/demo"
OUT="$1"

cp "$SRC/demo-fixtures.json" "$SRC/demo-credits.html" "$OUT/"
mkdir -p "$OUT/demo"
cp -r "$SRC/media/." "$OUT/demo/"

# Browsers load <img> URLs outside window.fetch(), so the photographs and the
# face crops are real static files at the paths the backend serves them from.
place() {
	for f in "$OUT/demo/$1"/*.webp; do
		name=$(basename "$f" .webp)
		mkdir -p "$OUT/$2/${name%-*}"
		mv "$f" "$OUT/$2/${name%-*}/${name##*-}"
	done
	rmdir "$OUT/demo/$1"
}
place photos api/photos
place faces api/photos/faces

cat >> "$OUT/_headers" <<'EOF'
/api/photos/*
  Content-Type: image/webp
  Cache-Control: public, max-age=3600
EOF
