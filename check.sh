#!/bin/sh
# Everything this repository checks about itself, in one command. The check is
# the same for every OPUS module and lives in opus-core.
set -eu
cd "$(dirname "$0")"
. backend/opus_core/ops/check.sh

# faces is Library's alone and lives outside the backend: its app is read by
# pyflakes and its lock audited the way the backend's are
opus_check_module() {
	docker compose exec -T backend python -m pyflakes < faces/app.py
	echo "faces pyflakes: clean"
	docker run --rm -v "$PWD/faces:/faces:ro" -v "$PWD/backend/opus_core/ops:/ops:ro" -w /faces \
		-v opus-check-pip:/root/.cache/pip python:3.14-slim sh -c '
		set -e
		pip install -q --root-user-action=ignore --disable-pip-version-check -r /ops/audit.txt
		pip-audit --disable-pip --progress-spinner off --vulnerability-service osv -r requirements.lock'
}

opus_check
