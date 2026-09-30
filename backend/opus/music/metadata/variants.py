"""The other mixes of an album, written down when a search we were running
anyway names one.

An Atmos or multichannel edition is not a higher grade of the stereo record,
it is a different reading of the same music — the owner plays it on different
hardware and would never want it stitched into the stereo album. So a mix is
noticed, never chased: nothing here searches, downloads, or touches a
release's own files and status. What is recorded is only what a payload already in
hand said, and where it said it.

Pure detection plus one upsert: a catalogue's album payload or a Discogs version
list in, a Sighting out."""

import re
from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from opus.models import ReleaseVariant

ATMOS = "atmos"
MULTICHANNEL = "multichannel"
SACD = "sacd"


class Sighting(NamedTuple):
    """One mix, as one source described it. `title` names the album so the
    caller can find the release it belongs to; `external_id` is the edition's
    own id at `source`."""

    variant: str
    source: str
    title: str
    external_id: str | None


# Discogs states the medium and the mix in one free-form format string
# ('SACD, Hybrid, Multichannel, Album', 'Blu-ray Audio, Album, Dolby Atmos');
# an edition can be several of these at once, so every pattern is tried
_DISCOGS_PATTERNS = (
    (ATMOS, re.compile(r"(?:dolby\s+)?atmos", re.I)),
    (MULTICHANNEL, re.compile(r"multichannel|quadraphonic|\bquad\b|surround|5\.1", re.I)),
    (SACD, re.compile(r"\bsacd\b", re.I)),
)


def from_catalog(album: dict) -> Sighting | None:
    """The spatial mix a catalogue's album payload names in `mixes`. A catalogue
    that files the Atmos edition as a SEPARATE album under the same title says
    so on that album, so the payload alone tells the two apart."""
    if ATMOS in (album.get("mixes") or ()):
        return Sighting(ATMOS, album["source"], album.get("title") or "",
                        _text(album.get("id")))
    return None


def from_discogs_versions(versions: list[dict], title: str) -> list[Sighting]:
    """The mixes among a master's editions. The album is named by its master,
    not by the version — a pressing's own title is often a translation ('El
    Lado Oscuro De La Luna') that matches no release we hold. One sighting per
    mix: a master with twenty SACD pressings is still one fact."""
    out: list[Sighting] = []
    seen: set[str] = set()
    for version in versions:
        for variant, pattern in _DISCOGS_PATTERNS:
            if variant in seen or not pattern.search(version.get("format") or ""):
                continue
            seen.add(variant)
            out.append(Sighting(variant, "discogs", title,
                                _text(version.get("id"))))
    return out


async def note(session, release_id: int, sighting: Sighting) -> None:
    """Record the mix beside the release. Idempotent per (release, mix,
    source): seeing it again refreshes where and when, it does not pile up."""
    statement = insert(ReleaseVariant).values(
        release_id=release_id, variant=sighting.variant,
        source=sighting.source, external_id=sighting.external_id,
    )
    await session.execute(statement.on_conflict_do_update(
        index_elements=["release_id", "variant", "source"],
        set_={"external_id": statement.excluded.external_id, "seen_at": func.now()},
    ))


def _text(value) -> str | None:
    return None if value is None else str(value)
