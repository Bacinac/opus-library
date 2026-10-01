#!/usr/bin/env bash
# Build the public OPUS demo — Player, Library and Downloads as three independent
# static origins, each the production frontend over a synthetic in-browser API —
# and gate every file it would publish. Nothing leaves this machine without --deploy.
#
#   ./deploy/demo.sh            build + gate into release/demo/{player,library,downloads}
#   ./deploy/demo.sh --deploy   additionally publish the three Cloudflare Pages projects
set -euo pipefail

LIBRARY="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="$(cd "$LIBRARY/.." && pwd)"
DOWNLOADS=${OPUS_DOWNLOADS_SOURCE:-$WORKSPACE/opus-downloads}
PLAYER=${OPUS_PLAYER_SOURCE:-$WORKSPACE/opus-player}
OUT="$LIBRARY/release/demo"

DEPLOY=0
for arg in "$@"; do
	case "$arg" in
		--deploy) DEPLOY=1 ;;
		-h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown option: $arg" >&2; exit 2 ;;
	esac
done

PLAYER_URL=${OPUS_DEMO_PLAYER_URL:-https://demo-opus.boskovic.biz}
LIBRARY_URL=${OPUS_DEMO_LIBRARY_URL:-https://demo-opus-library.boskovic.biz}
DOWNLOADS_URL=${OPUS_DEMO_DOWNLOADS_URL:-https://demo-opus-downloads.boskovic.biz}

GATE=$(git -C "$LIBRARY" config --get publicgate.command) \
	|| { echo "no publicgate.command in this clone — the demo is not built unchecked" >&2; exit 1; }
for apk in opus-player.apk opus-music.apk; do
	[[ -f "$PLAYER/android/dist/$apk" ]] || { echo "missing signed Android artifact: $PLAYER/android/dist/$apk" >&2; exit 1; }
done

rm -rf "$OUT"
mkdir -p "$OUT"

# Dependencies come from each lockfile, never from whatever a working tree last installed.
build() {
	local repo=$1 dest=$2; shift 2
	docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
		-v "$repo:$repo" -v "$OUT:$OUT" -w "$repo/frontend" "$@" node:24 \
		sh -c 'npm ci --no-audit --no-fund --loglevel=error && ./build-demo.sh "$1"' _ "$dest"
}
build "$PLAYER" "$OUT/player" -v "$LIBRARY:$LIBRARY:ro" -e OPUS_LIBRARY_SOURCE="$LIBRARY" -e VITE_OPUS_LIBRARY_URL="$LIBRARY_URL" -e VITE_OPUS_DOWNLOADS_URL="$DOWNLOADS_URL"
build "$LIBRARY" "$OUT/library" -e VITE_OPUS_DOWNLOADS_URL="$DOWNLOADS_URL" -e VITE_OPUS_PLAYER_URL="$PLAYER_URL"
build "$DOWNLOADS" "$OUT/downloads" -e VITE_OPUS_LIBRARY_URL="$LIBRARY_URL" -e VITE_OPUS_PLAYER_URL="$PLAYER_URL"

find "$OUT" -type f -print0 | xargs -0 "$GATE" scan --files

[[ $DEPLOY == 1 ]] || { echo "OPUS demo built and gated -> $OUT"; exit 0; }

TOKEN_FILE=${OPUS_CF_TOKEN_FILE:-$LIBRARY/.cf-token}
[[ -s "$TOKEN_FILE" ]] || { echo "Cloudflare token missing: $TOKEN_FILE" >&2; exit 1; }
CLOUDFLARE_API_TOKEN=$(tr -d ' \n\r\t' < "$TOKEN_FILE")
CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-3e7a41dadfd065fac67c6922c3f3b43e}
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID

publish() {
	docker run --rm -v "$OUT/$1:/site:ro" -e CLOUDFLARE_API_TOKEN -e CLOUDFLARE_ACCOUNT_ID node:24 \
		npx -y wrangler@latest pages deploy /site --project-name "$2" --branch main
}
publish player opus-demo
publish library opus-library-demo
publish downloads opus-downloads-demo
