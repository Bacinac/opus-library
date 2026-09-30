#!/usr/bin/env bash
# Produce the downloadable, self-contained source bundle used for a standalone
# installation. Tracked submodule files are included; secrets and volumes are not.
#
#   scripts/package-release.sh [version] [output-directory] [--dirty-ok]
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VERSION=$(node -p "require('./frontend/package.json').version")
OUT=$ROOT/release
DIRTY_OK=0
POSITIONAL=()
for arg in "$@"; do
	case "$arg" in
		--dirty-ok) DIRTY_OK=1 ;;
		-h|--help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		-*) echo "unknown option: $arg" >&2; exit 2 ;;
		*) POSITIONAL+=("$arg") ;;
	esac
done
[[ ${#POSITIONAL[@]} -lt 1 ]] || VERSION=${POSITIONAL[0]}
[[ ${#POSITIONAL[@]} -lt 2 ]] || OUT=${POSITIONAL[1]}
[[ ${#POSITIONAL[@]} -lt 3 ]] || { echo "too many arguments" >&2; exit 2; }

if [[ $DIRTY_OK == 0 ]] && ! git diff --quiet --ignore-submodules=dirty HEAD; then
	echo "working tree is dirty; commit first or pass --dirty-ok" >&2
	exit 1
fi

mkdir -p "$OUT"
ARCHIVE="$OUT/opus-library-$VERSION.tar.gz"
LIST=$(mktemp)
trap 'rm -f "$LIST"' EXIT
git ls-files --recurse-submodules -z > "$LIST"
# `--dirty-ok` is also useful while validating a release change before its
# commit. Include its non-ignored new files as well as modified tracked bytes.
if [[ $DIRTY_OK == 1 ]]; then
	git ls-files --others --exclude-standard -z >> "$LIST"
fi
tar --null -T "$LIST" --transform "s,^,opus-library-$VERSION/," -czf "$ARCHIVE"
(cd "$OUT" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")

echo "release bundle: $ARCHIVE"
echo "checksum:       $ARCHIVE.sha256"
