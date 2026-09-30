#!/usr/bin/env bash
# Build the public, backend-less OPUS Library demo.
#
# It is the production frontend with a synthetic in-browser API. Every write is
# made only to the page's in-memory fixture copy and disappears on reload.
# Nothing in the output knows a backend address or contains household data.
#
#   ./build-demo.sh [out-dir]
set -euo pipefail

UI="$(cd "$(dirname "$0")" && pwd)"
OUT="${1:-$UI/demo-dist}"

cd "$UI"
OPUS_DEMO=1 VITE_OPUS_DEMO=1 npm run build

rm -rf "$OUT"
mv build "$OUT"
. ../backend/opus_core/ops/revision.sh
opus_revision HEAD false > "$OUT/demo-version.json"

# Browsers load <img> URLs outside window.fetch(), so the synthetic photographs
# and face crops are real static files at the same paths as the backend serves.
PHOTO_IDS=$(node -e "const d=require('./static/demo-fixtures.json'); console.log(d.photos.map(x=>x.id).join(' '))")
for id in $PHOTO_IDS; do
	mkdir -p "$OUT/api/photos/$id"
	cp static/demo/photo.svg "$OUT/api/photos/$id/tile"
	cp static/demo/photo.svg "$OUT/api/photos/$id/preview"
done
FACE_IDS=$(node -e "const d=require('./static/demo-fixtures.json'); console.log(d.faces.map(x=>x.id).join(' '))")
for id in $FACE_IDS; do
	mkdir -p "$OUT/api/photos/faces/$id"
	cp static/demo/face.svg "$OUT/api/photos/faces/$id/crop"
	cp static/demo/face.svg "$OUT/api/photos/faces/$id/portrait"
done

cat > "$OUT/_headers" <<'EOF'
/api/photos/*
  Content-Type: image/svg+xml
  Cache-Control: public, max-age=3600
EOF

cat > "$OUT/_redirects" <<'EOF'
/*  /index.html  200
EOF

echo "OPUS demo built -> $OUT"
