#!/bin/sh
set -e
alembic upgrade head
if [ "${OPUS_DEV_RELOAD:-0}" = "1" ]; then
    exec uvicorn opus.main:app --host 0.0.0.0 --port 8095 --reload
fi
exec uvicorn opus.main:app --host 0.0.0.0 --port 8095
