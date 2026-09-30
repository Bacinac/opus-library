#!/bin/bash
# Deploy OPUS · Library to an instance from deploy/hosts.conf (default: prod).
# The steps are opus-core's ops/deploy.sh; this file names what only Library has.
set -euo pipefail
cd "$(dirname "$0")/.."

OPUS_MODULE=opus-library
# faces is rebuilt with them: it is code in this repo like the rest, and leaving
# it out meant a change to the ML service reached production only by accident,
# whenever something else happened to recreate it
OPUS_SERVICES=(backend frontend faces)
OPUS_HEALTH_PORTS=(8095)
OPUS_VOLUME_OWNER=(opus_library_backend dirs /downloads /music /movies /television /video /derivatives /vault /photos-write)

source backend/opus_core/ops/deploy.sh
opus_deploy "$@"
