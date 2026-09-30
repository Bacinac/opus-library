#!/usr/bin/env bash
# Publish an OPUS release on Bacinac/opus-library: the pushed Library HEAD becomes
# tag v<version>, and its GitHub Release carries the suite archive and the OPUS TV
# and OPUS Music APKs that boskovic.biz links. The release is refused until a
# stranger without an account can clone all three modules and every submodule
# they name; the archive itself is gated by scripts/package-suite.sh.
#
#   ./deploy/release.sh            publish
#   ./deploy/release.sh --dry-run  check and package everything, publish nothing
set -euo pipefail

LIBRARY="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="$(cd "$LIBRARY/.." && pwd)"
DOWNLOADS=${OPUS_DOWNLOADS_SOURCE:-$WORKSPACE/opus-downloads}
PLAYER=${OPUS_PLAYER_SOURCE:-$WORKSPACE/opus-player}
REPO=Bacinac/opus-library

DRY_RUN=0
case "${1:-}" in
	"") ;;
	--dry-run) DRY_RUN=1 ;;
	-h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
	*) echo "unknown option: $1" >&2; exit 2 ;;
esac

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

cd "$LIBRARY"
[[ -z "$(git status --porcelain)" ]] || die "uncommitted changes — a release is exactly what origin/main holds"
git fetch -q --tags origin main
SHA=$(git rev-parse HEAD)
[[ "$SHA" == "$(git rev-parse origin/main)" ]] || die "HEAD is not origin/main"
VERSION="$(tr -d ' \t\n\r' < VERSION).$(git rev-list --count HEAD)"
TAG="v$VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"

# Asked anonymously, as the stranger unpacking the suite would.
public_commit() { curl -fsS -o /dev/null "https://api.github.com/repos/$1/commits/$2"; }
public_tree() {
	local repo=$1 slug=$2 head key url name path pin
	head=$(git -C "$repo" rev-parse HEAD)
	public_commit "$slug" "$head" || die "$slug@${head:0:8} is not publicly readable"
	while read -r key url; do
		name=${key#submodule.}; name=${name%.url}
		path=$(git -C "$repo" config -f .gitmodules "submodule.$name.path")
		pin=$(git -C "$repo" ls-tree HEAD "$path" | awk '{print $3}')
		[[ "$url" =~ ^https://github\.com/([^/]+/[^/]+)$ ]] || die "$slug submodule $path: $url is not a public https URL"
		public_commit "${BASH_REMATCH[1]%.git}" "$pin" || die "$slug submodule $path@${pin:0:8} is not publicly readable"
	done < <(git -C "$repo" config -f .gitmodules --get-regexp '\.url$')
}
public_tree "$LIBRARY" "$REPO"
public_tree "$DOWNLOADS" Bacinac/opus-downloads
public_tree "$PLAYER" Bacinac/opus-player
ok "all three modules and their submodules are public"

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
scripts/package-suite.sh "$OUT" || die "package-suite.sh refused the suite"
SUITE="$OUT/opus-suite-$VERSION.tar.gz"
[[ -f "$SUITE" ]] || die "package-suite.sh did not produce $(basename "$SUITE")"
ASSETS=("$SUITE" "$SUITE.sha256" "$OUT"/opus-tv-*.apk "$OUT"/opus-music-*.apk)
ok "packaged and gated: $(cd "$OUT" && ls opus-suite-*.tar.gz opus-tv-*.apk opus-music-*.apk | tr '\n' ' ')"

PREVIOUS=$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$SHA^" 2>/dev/null || true)
if [[ -n "$PREVIOUS" ]]; then
	NOTES=$(git log --no-merges --format='- %s' "$PREVIOUS..$SHA")
else
	NOTES="First public release."
fi

if (( DRY_RUN )); then
	printf 'would tag %s as %s and release it on %s with these notes:\n%s\n' "${SHA:0:8}" "$TAG" "$REPO" "$NOTES"
	exit 0
fi
gh release create "$TAG" "${ASSETS[@]}" --repo "$REPO" --target "$SHA" --latest --title "OPUS $TAG" --notes "$NOTES" >/dev/null
git fetch -q origin "refs/tags/$TAG:refs/tags/$TAG"
ok "released $TAG — rebuild and deploy boskovic.biz to show it"
