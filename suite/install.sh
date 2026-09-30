#!/usr/bin/env bash
# Install an OPUS Suite role from the combined release bundle.
#
#   ./install.sh --role=library-downloads
#   ./install.sh --role=player --enrollment=/secure/path/opus-player.enrollment.env
#   ./install.sh --role=all
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
for dir in library downloads player; do
	[[ -d "$ROOT/$dir" ]] || { echo "missing $dir/; run this installer from an OPUS Suite bundle" >&2; exit 1; }
done
source library/backend/opus_core/ops/install.sh

ROLE=
HOST_ADDRESS=
LIBRARY_URL=
LIBRARY_PUBLIC_URL=
DOWNLOADS_PUBLIC_URL=
PLAYER_PUBLIC_URL=
ENROLLMENT=
MEDIA_ROOT=
for arg in "$@"; do
	case "$arg" in
		--role=*) ROLE=${arg#*=} ;;
		--host-address=*) HOST_ADDRESS=${arg#*=} ;;
		--library-url=*) LIBRARY_URL=${arg#*=} ;;
		--library-public-url=*) LIBRARY_PUBLIC_URL=${arg#*=} ;;
		--downloads-public-url=*) DOWNLOADS_PUBLIC_URL=${arg#*=} ;;
		--player-public-url=*) PLAYER_PUBLIC_URL=${arg#*=} ;;
		--enrollment=*) ENROLLMENT=${arg#*=} ;;
		--media-root=*) MEDIA_ROOT=${arg#*=} ;;
		-h|--help) opus_help; exit 0 ;;
		*) echo "unknown option: $arg" >&2; exit 2 ;;
	esac
done
case "$ROLE" in library-downloads|player|all) ;; *) echo "use --role=library-downloads, --role=player or --role=all" >&2; exit 2 ;; esac
opus_require python3 curl

HOST_ADDRESS=${HOST_ADDRESS:-$(opus_host_address)}
LIBRARY_URL=${LIBRARY_URL:-http://$HOST_ADDRESS:8095}
DOWNLOADS_URL=http://$HOST_ADDRESS:8097
LIBRARY_PUBLIC_URL=${LIBRARY_PUBLIC_URL:-http://$HOST_ADDRESS:5280}
DOWNLOADS_PUBLIC_URL=${DOWNLOADS_PUBLIC_URL:-http://$HOST_ADDRESS:5282}
if [[ -z "$PLAYER_PUBLIC_URL" && ("$ROLE" == player || "$ROLE" == all) ]]; then
	PLAYER_PUBLIC_URL=http://$HOST_ADDRESS:5283
fi

json_value() {
	python3 -c 'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}
held_token() {
	python3 -c 'import json,sys; print(next((r["token"] for r in json.load(sys.stdin)["tokens"] if r["consumer"] == sys.argv[1]), ""))' "$1"
}
# The token one consumer calls a module with; issued when there is none yet.
consumer_token() {
	local url=$1 consumer=$2 cookie=$3 held
	held=$(curl -fsS -b "$cookie" "$url/api/auth/token" | held_token "$consumer")
	if [[ -z "$held" ]]; then
		held=$(curl -fsS -b "$cookie" -X POST "$url/api/auth/token" -H 'Content-Type: application/json' \
			--data "{\"consumer\": \"$consumer\"}" | json_value token)
	fi
	[[ -n "$held" ]] || { echo "$url issued no token for $consumer" >&2; exit 1; }
	printf '%s' "$held"
}

install_storage() {
	echo "Installing OPUS Library + Downloads on $HOST_ADDRESS"
	VITE_OPUS_DOWNLOADS_URL="$DOWNLOADS_PUBLIC_URL" \
	VITE_OPUS_PLAYER_URL="$PLAYER_PUBLIC_URL" \
		"$ROOT/library/install.sh"

	local username=${OPUS_ADMIN_USERNAME:-}
	local password=${OPUS_ADMIN_PASSWORD:-}
	if [[ -z "$username" && -f "$ROOT/library/.install-admin" ]]; then
		username=$(awk -F': ' '$1 == "username" {print $2}' "$ROOT/library/.install-admin")
		password=$(awk -F': ' '$1 == "password" {print $2}' "$ROOT/library/.install-admin")
	fi
	[[ -n "$username" && -n "$password" ]] || {
		echo "Set OPUS_ADMIN_USERNAME and OPUS_ADMIN_PASSWORD to connect an existing Library install." >&2
		exit 1
	}

	local cookie login player_token downloads_token library_token payload
	cookie=$(mktemp)
	trap 'rm -f "$cookie"' RETURN
	login=$(python3 - "$username" "$password" <<'PY'
import json, sys
print(json.dumps({"username": sys.argv[1], "password": sys.argv[2]}))
PY
)
	curl -fsS -c "$cookie" -X POST "$LIBRARY_URL/api/auth/login" -H 'Content-Type: application/json' --data "$login" >/dev/null
	player_token=$(consumer_token "$LIBRARY_URL" player "$cookie")
	downloads_token=$(consumer_token "$LIBRARY_URL" downloads "$cookie")

	local session_key
	session_key=$(env_read OPUS_SESSION_KEY "$ROOT/library/.env")
	OPUS_AUTH_URL="$LIBRARY_URL" \
	OPUS_AUTH_TOKEN="$downloads_token" \
	OPUS_SESSION_KEY="$session_key" \
	OPUS_LANDING_DEVICE="$ROOT/library/volumes/downloads" \
	OPUS_MEDIA_HOST_DIR="$ROOT/library/volumes" \
	VITE_OPUS_LIBRARY_URL="$LIBRARY_PUBLIC_URL" \
	VITE_OPUS_PLAYER_URL="$PLAYER_PUBLIC_URL" \
		"$ROOT/downloads/install.sh"

	# the Library session opens Downloads too: both sign with the same key
	library_token=$(consumer_token "$DOWNLOADS_URL" library "$cookie")
	payload=$(python3 - "$DOWNLOADS_URL" "$library_token" <<'PY'
import json, sys
print(json.dumps({
    "opus_url": sys.argv[1],
    "opus_token": sys.argv[2],
    "opus_landing_root": "/landing",
    "opus_landing_dir": "/downloads"
}))
PY
)
	curl -fsS -b "$cookie" -X PUT "$LIBRARY_URL/api/settings" -H 'Content-Type: application/json' --data "$payload" >/dev/null

	ENROLLMENT=${ENROLLMENT:-$ROOT/opus-player.enrollment.env}
	python3 - "$ENROLLMENT" "$LIBRARY_URL" "$player_token" "$session_key" "$LIBRARY_PUBLIC_URL" "$DOWNLOADS_PUBLIC_URL" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
values = {
    "OPUS_LIBRARY_URL": sys.argv[2],
    "OPUS_LIBRARY_TOKEN": sys.argv[3],
    "OPUS_SESSION_KEY": sys.argv[4],
    "VITE_OPUS_LIBRARY_URL": sys.argv[5],
    "VITE_OPUS_DOWNLOADS_URL": sys.argv[6],
}
path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
path.chmod(0o600)
PY
	trap - RETURN
	rm -f "$cookie"
	echo
	echo "Storage role is ready:"
	echo "  Library:   $LIBRARY_PUBLIC_URL"
	echo "  Downloads: $DOWNLOADS_PUBLIC_URL"
	echo "Player enrollment: $ENROLLMENT (mode 0600; copy it securely to the playback server)"
}

install_player() {
	if [[ "$ROLE" == all ]]; then
		ENROLLMENT=${ENROLLMENT:-$ROOT/opus-player.enrollment.env}
	fi
	[[ -n "$ENROLLMENT" ]] || { echo "player role requires --enrollment=FILE" >&2; exit 1; }
	[[ -f "$ENROLLMENT" ]] || { echo "enrollment file not found: $ENROLLMENT" >&2; exit 1; }
	echo "Installing OPUS Player on $HOST_ADDRESS"
	local args=("--enrollment=$ENROLLMENT")
	[[ -z "$MEDIA_ROOT" ]] || args+=("--media-root=$MEDIA_ROOT")
	"$ROOT/player/install.sh" "${args[@]}"
	echo "  Player: $PLAYER_PUBLIC_URL"
}

case "$ROLE" in
	library-downloads) install_storage ;;
	player) install_player ;;
	all) install_storage; MEDIA_ROOT=${MEDIA_ROOT:-$ROOT/library/volumes}; install_player ;;
esac
