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

cp demo/demo-net.js "$OUT/"
./demo-catalogue.sh "$OUT"

cat > "$OUT/_redirects" <<'EOF'
/*  /index.html  200
EOF

echo "OPUS demo built -> $OUT"
