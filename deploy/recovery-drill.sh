#!/bin/bash
# Restore a Library database archive into a disposable, network-isolated
# PostgreSQL container. It never reads .env, talks to a host, or mounts a
# production volume. Usage: bash deploy/recovery-drill.sh archive.dump
set -euo pipefail

image="pgvector/pgvector:pg18-trixie"
archive="${1:-}"
[[ -n "$archive" && -f "$archive" ]] || {
    echo "usage: $0 /safe/path/opus-library-*.dump" >&2
    exit 2
}
[[ $# -eq 1 ]] || { echo "only one archive path is accepted" >&2; exit 2; }

sidecar="${archive}.sha256"
[[ -f "$sidecar" ]] || {
    echo "missing SHA-256 sidecar: $sidecar" >&2
    exit 2
}

# sha256sum's standard sidecar format stores a basename, so check it in the
# archive directory and never have to interpolate an untrusted full path.
archive_dir="$(cd "$(dirname "$archive")" && pwd)"
archive_name="$(basename "$archive")"
(cd "$archive_dir" && sha256sum -c "$(basename "$sidecar")")

name="opus-library-recovery-${$}"
password="$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n')"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

echo "==> validating archive structure"
docker run --rm -i "$image" pg_restore --list <"$archive" >/dev/null

echo "==> starting isolated PostgreSQL"
# PostgreSQL 18 stores a versioned data directory below this path; mounting the
# old /data path makes the image reject an apparently upgraded cluster.
docker run -d --rm --name "$name" --network none \
    --tmpfs /var/lib/postgresql:rw,noexec,nosuid,size=1g \
    -e POSTGRES_USER=opus -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=opus \
    "$image" >/dev/null
for _ in $(seq 1 30); do
    if docker exec "$name" pg_isready -U opus -d opus >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "$name" pg_isready -U opus -d opus >/dev/null

echo "==> restoring into disposable database"
docker exec -i "$name" pg_restore -U opus -d opus --clean --if-exists \
    --no-owner --no-privileges <"$archive"

revision="$(docker exec "$name" psql -U opus -d opus -v ON_ERROR_STOP=1 -Atc \
    'SELECT version_num FROM alembic_version')"
tables="$(docker exec "$name" psql -U opus -d opus -v ON_ERROR_STOP=1 -Atc \
    "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
[[ "$revision" =~ ^[A-Za-z0-9_]+$ ]] || { echo "invalid Alembic revision" >&2; exit 1; }
[[ "$tables" =~ ^[0-9]+$ && "$tables" -gt 0 ]] || { echo "no application tables restored" >&2; exit 1; }

echo "restore drill: passed (revision $revision; $tables public tables)"
