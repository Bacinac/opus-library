#!/bin/bash
# Create and validate an OPUS · Library PostgreSQL backup without copying a
# password to this machine.  Usage: bash deploy/backup.sh [instance] [output-dir]
set -euo pipefail

cd "$(dirname "$0")/.."

instance="${1:-prod}"
destination="${2:-/mnt/docker/_exports/deploy-backups}"
read -r _ ssh_host lxc_id _ _ < <(
    grep -E "^${instance}[[:space:]]" deploy/hosts.conf
) || { echo "unknown instance: ${instance}" >&2; exit 1; }
[[ "$lxc_id" =~ ^[0-9]+$ ]] || { echo "invalid LXC id in deploy/hosts.conf" >&2; exit 2; }

# Dumps contain account data and service configuration. Keep them outside the
# checkout, unreadable by other local users, and never print their content.
umask 077
mkdir -p "$destination"
chmod 700 "$destination"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive="$destination/opus-library-${instance}-${stamp}.dump"
partial="${archive}.partial"
trap 'rm -f "$partial"' EXIT

echo "==> dumping Library database from ${instance}"
ssh "$ssh_host" "sudo pct exec ${lxc_id} -- docker exec -i opus_library_postgres \\
    pg_dump -U opus --format=custom --no-owner --no-privileges opus" >"$partial"
test -s "$partial" || { echo "database dump is empty" >&2; exit 1; }

echo "==> validating archive structure"
if command -v pg_restore >/dev/null; then
    pg_restore --list "$partial" >/dev/null
elif command -v docker >/dev/null; then
    # Match the server major version. This reads the archive only; it does not
    # create a database or contact the production host.
    docker run --rm -i pgvector/pgvector:pg18-trixie pg_restore --list <"$partial" >/dev/null
else
    echo "need pg_restore or docker to validate the backup" >&2
    exit 1
fi

mv "$partial" "$archive"
sha256sum "$archive" >"${archive}.sha256"
trap - EXIT
echo "backup: ${archive}"
