#!/bin/sh
set -e
case "$OPUS_ROLE" in
    production|development) ;;
    *) echo "OPUS_ROLE must be production or development, not '$OPUS_ROLE'" >&2; exit 1 ;;
esac
alembic upgrade head
python -m opus.photos.vault.migrate
python -m opus.importing
if [ "$OPUS_ROLE" = development ]; then
    exec uvicorn opus.main:app --host 0.0.0.0 --port 8095 --reload
fi
exec uvicorn opus.main:app --host 0.0.0.0 --port 8095
