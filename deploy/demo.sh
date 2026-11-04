#!/usr/bin/env bash
# Build the public OPUS demo — Player, Library and Downloads as three independent
# static origins, each the production frontend over a synthetic in-browser API —
# walk every page of it in a browser, and gate every file it would publish.
# Nothing leaves this machine without --deploy.
#
#   ./deploy/demo.sh            build, walk and gate into release/demo/{player,library,downloads}
#   ./deploy/demo.sh --shots    also retake each module's docs/screenshots from the walked demo
#   ./deploy/demo.sh --deploy   additionally publish the three Cloudflare Pages projects
set -euo pipefail

LIBRARY="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="$(cd "$LIBRARY/.." && pwd)"
DOWNLOADS=${OPUS_DOWNLOADS_SOURCE:-$WORKSPACE/opus-downloads}
PLAYER=${OPUS_PLAYER_SOURCE:-$WORKSPACE/opus-player}
OUT="$LIBRARY/release/demo"

DEPLOY=0
SHOTS=0
for arg in "$@"; do
	case "$arg" in
		--deploy) DEPLOY=1 ;;
		--shots) SHOTS=1 ;;
		-h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
	docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e NPM_CONFIG_UPDATE_NOTIFIER=false \
		-v "$repo:$repo" -v "$OUT:$OUT" -w "$repo/frontend" "$@" node:24 \
		sh -c 'npm ci --no-audit --no-fund --loglevel=error && ./build-demo.sh "$1"' _ "$dest"
}
build "$PLAYER" "$OUT/player" -v "$LIBRARY:$LIBRARY:ro" -e OPUS_LIBRARY_SOURCE="$LIBRARY" -e VITE_OPUS_LIBRARY_URL="$LIBRARY_URL" -e VITE_OPUS_DOWNLOADS_URL="$DOWNLOADS_URL"
build "$LIBRARY" "$OUT/library" -e VITE_OPUS_DOWNLOADS_URL="$DOWNLOADS_URL" -e VITE_OPUS_PLAYER_URL="$PLAYER_URL"
build "$DOWNLOADS" "$OUT/downloads" -e VITE_OPUS_LIBRARY_URL="$LIBRARY_URL" -e VITE_OPUS_PLAYER_URL="$PLAYER_URL"

# A page the synthetic API cannot answer, a missing picture or a page error
# fails the build here rather than in front of a visitor. The image's version
# is the playwright-core version in demo-walk/package-lock.json.
WALK="$LIBRARY/deploy/demo-walk"
walk() {
	local command=$1; shift
	docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e NPM_CONFIG_UPDATE_NOTIFIER=false \
		-v "$LIBRARY:$LIBRARY:ro" -v "$PLAYER:$PLAYER:ro" -v "$DOWNLOADS:$DOWNLOADS:ro" "$@" \
		mcr.microsoft.com/playwright:v1.64.1-noble \
		sh -c 'mkdir -p /tmp/walk && cp "$1/package.json" "$1/package-lock.json" /tmp/walk/ \
			&& npm ci --prefix /tmp/walk --no-audit --no-fund --loglevel=error >/dev/null \
			&& DEMO_WALK_PREFIX=/tmp/walk node "$1/walk.mjs" "$2" "$3" "$4" "$5"' _ "$WALK" "$command" \
		"player=$OUT/player=$PLAYER/frontend/src/routes=$PLAYER_URL" \
		"library=$OUT/library=$LIBRARY/frontend/src/routes=$LIBRARY_URL" \
		"downloads=$OUT/downloads=$DOWNLOADS/frontend/src/routes=$DOWNLOADS_URL"
}
walk check
if [[ $SHOTS == 1 ]]; then
	walk shots -v "$PLAYER/docs/screenshots:$PLAYER/docs/screenshots" \
		-v "$LIBRARY/docs/screenshots:$LIBRARY/docs/screenshots" \
		-v "$DOWNLOADS/docs/screenshots:$DOWNLOADS/docs/screenshots"
fi

card() {
	local repo=$1 dest=$2 origin=$3 title=$4 description=$5
	docker run --rm --user "$(id -u):$(id -g)" -v "$repo:$repo:ro" -v "$OUT:$OUT" node:24 \
		node "$repo/frontend/src/lib/kit/linkcard.mjs" "$OUT/$dest" "$origin" \
		"$repo/docs/screenshots/social-preview.png" "$title" "$description"
}
card "$PLAYER" player "$PLAYER_URL" "OPUS · Player — live demo" \
	"One frontend for every screen in the house: films, series, records and the family album. A public demo over an invented household, nothing to install."
card "$LIBRARY" library "$LIBRARY_URL" "OPUS · Library — live demo" \
	"One library for music, films, series and photographs: artists and series followed, releases judged, faces gathered into people across the years. A public demo over an invented household."
card "$DOWNLOADS" downloads "$DOWNLOADS_URL" "OPUS · Downloads — live demo" \
	"One interface over Usenet, torrents, Soulseek and web video: one search, one queue. A public demo over synthetic data."

find "$OUT" -type f -print0 | xargs -0 "$GATE" scan --files

[[ $DEPLOY == 1 ]] || { echo "OPUS demo built, walked and gated -> $OUT"; exit 0; }

TOKEN_FILE=${OPUS_CF_TOKEN_FILE:-$LIBRARY/.cf-token}
[[ -s "$TOKEN_FILE" ]] || { echo "Cloudflare token missing: $TOKEN_FILE" >&2; exit 1; }
CLOUDFLARE_API_TOKEN=$(tr -d ' \n\r\t' < "$TOKEN_FILE")
CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-3e7a41dadfd065fac67c6922c3f3b43e}
export CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID

publish() {
	docker run --rm -v "$OUT/$1:/site:ro" -e CLOUDFLARE_API_TOKEN -e CLOUDFLARE_ACCOUNT_ID -e NPM_CONFIG_UPDATE_NOTIFIER=false node:24 \
		npx -y wrangler@latest pages deploy /site --project-name "$2" --branch main
}
publish player opus-demo
publish library opus-library-demo
publish downloads opus-downloads-demo
