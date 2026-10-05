#!/usr/bin/env bash
# OPUS Library installer — from a fresh release bundle or clone to a running app.
#
#   ./install.sh             prepare, build, start and create the first admin
#   ./install.sh --no-start  only prepare .env/directories and validate Compose
#   ./install.sh --help
#
# Re-running is safe: existing secrets, media and database contents are kept.
set -euo pipefail
cd "$(dirname "$0")"

source backend/opus_core/ops/install.sh

START=1
for arg in "$@"; do
	case "$arg" in
		--no-start) START=0 ;;
		-h|--help) opus_help; exit 0 ;;
		*) echo "unknown option: $arg" >&2; exit 2 ;;
	esac
done

opus_require docker python3 curl
opus_env_file
opus_generate POSTGRES_PASSWORD:24 OPUS_SESSION_KEY:32

# The first start needs one narrow credential to create the administrator. It is
# removed from the environment immediately after that account exists.
opus_generate OPUS_BOOTSTRAP_KEY:32

ROOT=$(pwd -P)
for volume in downloads music movies television video photos derivatives vault; do
	mkdir -p "$ROOT/volumes/$volume"
done
mkdir -p "$ROOT/volumes/postgres"

for mount in DOWNLOADS:downloads MUSIC:music MOVIES:movies TV:television VIDEO:video PHOTOS:photos DERIVATIVES:derivatives VAULT:vault; do
	key=OPUS_${mount%%:*}_DEVICE
	value=${!key:-$(env_read "$key")}
	env_set "$key" "${value:-$ROOT/volumes/${mount##*:}}"
done
opus_production
opus_pass_through VITE_OPUS_DOWNLOADS_URL VITE_OPUS_PLAYER_URL OPUS_COOKIE_DOMAIN

opus_validate "OPUS Library"
[[ $START == 1 ]] || exit 0

echo "building OPUS Library"
opus_build backend frontend
compose up -d postgres backend frontend

echo "waiting for the database and API"
opus_wait http://127.0.0.1:8095/api/ready 120 "OPUS API"

SESSION=$(curl -fsS http://127.0.0.1:8095/api/auth/session)
NEEDS_ADMIN=$(python3 -c 'import json,sys; print("1" if json.load(sys.stdin).get("bootstrap") else "0")' <<<"$SESSION")
CREDS=.install-admin
if [[ "$NEEDS_ADMIN" == 1 ]]; then
	ADMIN_USER=${OPUS_ADMIN_USERNAME:-admin}
	ADMIN_PASSWORD=${OPUS_ADMIN_PASSWORD:-$(generate 12)}
	BOOTSTRAP_KEY=$(env_read OPUS_BOOTSTRAP_KEY)
	PAYLOAD=$(python3 - "$ADMIN_USER" "$ADMIN_PASSWORD" <<'PY'
import json, sys
print(json.dumps({"name": sys.argv[1], "display": "OPUS administrator", "password": sys.argv[2], "role": "admin"}))
PY
)
	curl -fsS -X POST http://127.0.0.1:8095/api/auth/bootstrap \
		-H 'Content-Type: application/json' \
		-H "X-OPUS-Bootstrap-Key: $BOOTSTRAP_KEY" \
		--data "$PAYLOAD" >/dev/null
	{
		printf 'OPUS administrator\nusername: %s\npassword: %s\n' "$ADMIN_USER" "$ADMIN_PASSWORD"
	} > "$CREDS"
	chmod 600 "$CREDS"
	echo "created the first administrator"
fi

# The credential has completed its only job. Recreate only the backend so it no
# longer exists in the running process or its container configuration.
env_set OPUS_BOOTSTRAP_KEY ""
compose up -d --force-recreate backend >/dev/null

opus_wait http://127.0.0.1:8095/api/ready 60 "OPUS API"
opus_wait http://127.0.0.1:5280/ 60 "OPUS UI"

HOST=$(opus_host_address)
echo
echo "OPUS Library is ready: http://$HOST:5280"
if [[ -f "$CREDS" ]]; then
	echo "Administrator credentials: $ROOT/$CREDS (mode 0600)"
fi
echo "Configure integrations and library policy in Settings."
echo "Copy media into $ROOT/volumes/{music,movies,television,video,photos}."
