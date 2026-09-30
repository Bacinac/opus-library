#!/usr/bin/env bash
# Build one self-contained OPUS Suite source archive from the three sibling repos,
# with the signed Android apps included and checksummed, and gate every file it
# carries. The archive is exactly what the three repos have pushed: a dirty or
# unpushed checkout, or an APK built from anything but the pushed Player, is refused.
#
#   scripts/package-suite.sh [out-dir]    default: release/
set -euo pipefail
LIBRARY="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="$(cd "$LIBRARY/.." && pwd)"
DOWNLOADS=${OPUS_DOWNLOADS_SOURCE:-$WORKSPACE/opus-downloads}
PLAYER=${OPUS_PLAYER_SOURCE:-$WORKSPACE/opus-player}
OUT=${1:-$LIBRARY/release}

die() { echo "$*" >&2; exit 1; }

GATE=$(git -C "$LIBRARY" config --get publicgate.command) \
	|| die "no publicgate.command in this clone — the suite is not packaged unchecked"
for repo in "$LIBRARY" "$DOWNLOADS" "$PLAYER"; do
	[[ -d "$repo/.git" ]] || die "not a git repository: $repo"
	[[ -z "$(git -C "$repo" status --porcelain)" ]] || die "uncommitted changes in $repo"
	git -C "$repo" fetch -q origin
	git -C "$repo" merge-base --is-ancestor HEAD origin/main || die "$repo: HEAD is not pushed to origin/main"
done
for app in apk music; do
	meta="$PLAYER/android/dist/$app.json"
	[[ -f "$meta" ]] || die "missing signed Android artifact: $meta"
	built=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sourceRevision"])' "$meta")
	git -C "$PLAYER" merge-base --is-ancestor "$built" HEAD 2>/dev/null \
		|| die "$meta was built from ${built:0:8}, which is not in the Player's history — rebuild with android/build.sh"
	git -C "$PLAYER" diff --quiet "$built" HEAD -- android \
		|| die "$meta predates changes to android/ — rebuild with android/build.sh"
done
for apk in "$PLAYER/android/dist/opus-player.apk" "$PLAYER/android/dist/opus-music.apk"; do
	[[ -f "$apk" ]] || die "missing signed Android artifact: $apk"
done

VERSION="$(tr -d ' \t\r\n' < "$LIBRARY/VERSION").$(git -C "$LIBRARY" rev-list --count HEAD)"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
NAME=opus-suite-$VERSION
mkdir -p "$STAGE/$NAME" "$OUT"
copy_repo() {
	local source=$1 destination=$2 list
	list=$(mktemp)
	git -C "$source" ls-files --recurse-submodules -z > "$list"
	mkdir -p "$destination"
	(cd "$source" && tar --null -T "$list" -cf -) | tar -C "$destination" -xf -
	rm -f "$list"
}
copy_repo "$LIBRARY" "$STAGE/$NAME/library"
copy_repo "$DOWNLOADS" "$STAGE/$NAME/downloads"
copy_repo "$PLAYER" "$STAGE/$NAME/player"
mkdir -p "$STAGE/$NAME/player/android/dist"
cp "$PLAYER/android/dist/opus-player.apk" "$STAGE/$NAME/player/android/dist/opus-player.apk"
cp "$PLAYER/android/dist/opus-player.apk" "$STAGE/$NAME/player/android/dist/opus-tv.apk"
cp "$PLAYER/android/dist/opus-music.apk" "$STAGE/$NAME/player/android/dist/opus-music.apk"
cp "$PLAYER/android/dist/apk.json" "$STAGE/$NAME/player/android/dist/apk.json"
cp "$PLAYER/android/dist/music.json" "$STAGE/$NAME/player/android/dist/music.json"
cp "$LIBRARY/suite/install.sh" "$STAGE/$NAME/install.sh"
cp "$LIBRARY/suite/README.md" "$STAGE/$NAME/README.md"
printf 'library %s\ndownloads %s\nplayer %s\n' \
	"$(git -C "$LIBRARY" rev-parse HEAD)" "$(git -C "$DOWNLOADS" rev-parse HEAD)" "$(git -C "$PLAYER" rev-parse HEAD)" \
	> "$STAGE/$NAME/SOURCES"
chmod +x "$STAGE/$NAME/install.sh" "$STAGE/$NAME/library/install.sh" "$STAGE/$NAME/downloads/install.sh" "$STAGE/$NAME/player/install.sh"
(
	cd "$STAGE/$NAME"
	sha256sum player/android/dist/opus-player.apk player/android/dist/opus-tv.apk player/android/dist/opus-music.apk > SHA256SUMS
)

# Every repo tree, submodules included, is gated with its own repo's allow list;
# a path-blind scan of the staged copy would misreport their test fixtures. What
# only the archive carries is gated as loose files: the APKs are zip archives, so
# the gate reads what is inside them.
for repo in "$LIBRARY" "$DOWNLOADS" "$PLAYER"; do
	mapfile -t subs < <(git -C "$repo" submodule foreach --quiet --recursive 'echo "$toplevel/$sm_path"')
	for tree in "$repo" "${subs[@]}"; do
		"$GATE" scan "$tree"
	done
done
for apk in opus-player opus-music; do
	mkdir -p "$STAGE/unpacked/$apk"
	python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
		"$PLAYER/android/dist/$apk.apk" "$STAGE/unpacked/$apk"
done
{
	find "$STAGE/$NAME" -maxdepth 1 -type f -print0
	find "$STAGE/$NAME/player/android/dist" "$STAGE/unpacked" -type f -print0
} | xargs -0 "$GATE" scan --files

ARCHIVE="$OUT/$NAME.tar.gz"
tar -C "$STAGE" -czf "$ARCHIVE" "$NAME"
(cd "$OUT" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256")
# Also publish the two installable apps as direct release assets. The TV name is
# the product-facing alias of the signed Player APK; their bytes and signer stay
# identical, so Android upgrades the same package rather than installing a fork.
TV_VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["versionName"].lstrip("v"))' "$PLAYER/android/dist/apk.json")
MUSIC_VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["versionName"].lstrip("v"))' "$PLAYER/android/dist/music.json")
TV_ASSET="$OUT/opus-tv-$TV_VERSION.apk"
MUSIC_ASSET="$OUT/opus-music-$MUSIC_VERSION.apk"
cp "$PLAYER/android/dist/opus-player.apk" "$TV_ASSET"
cp "$PLAYER/android/dist/opus-music.apk" "$MUSIC_ASSET"
(cd "$OUT" && sha256sum "$(basename "$TV_ASSET")" > "$(basename "$TV_ASSET").sha256")
(cd "$OUT" && sha256sum "$(basename "$MUSIC_ASSET")" > "$(basename "$MUSIC_ASSET").sha256")
echo "suite bundle: $ARCHIVE"
echo "checksum:     $ARCHIVE.sha256"
echo "OPUS TV:     $TV_ASSET"
echo "OPUS Music:  $MUSIC_ASSET"
