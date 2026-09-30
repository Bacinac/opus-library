"""Usenet channel — the search and the grab happen in OPUS now.

The music half used to drive Prowlarr and SABnzbd itself: two API dialects, two API keys,
two URLs, and its own handling of SABnzbd's queue, history and archive. All of
that is mechanism, so it moved to OPUS · Downloads and this channel is a client
of `/search` + `/inspect` + `/grab` + `/jobs`.

What stays here is judgement, and there is more of it than usual for usenet:
reading the format out of the indexer's own category, and deciding what a post's
declared file list means. OPUS reports the names it found in the NZB and never a
verdict — `nzb.classify` is what turns that into audio, packed or obfuscated."""


from opus import acquire
from opus.music.channels import nzb
from opus.acquire import JobStatus
from opus.music.channels.base import Candidate, CandidateFile, Channel, Contents

NAMESPACE = "music"

# Newznab audio subcategories — the indexer's own format label, far more reliable
# than parsing the release title (many FLAC albums never say "FLAC" in the title).
# When the result sits only in the parent 3000 category the title is all there
# is, and reading it is opus.music.matching.quality's job, not this channel's.
LOSSLESS_CATEGORY = 3040  # Audio/Lossless (FLAC, ALAC, WAV, …)
MP3_CATEGORY = 3010       # Audio/MP3


def _extension_from_categories(category_ids: list[int]) -> str:
    if LOSSLESS_CATEGORY in category_ids:
        return ".flac"
    if MP3_CATEGORY in category_ids:
        return ".mp3"
    return ""


class SabnzbdChannel(Channel):
    name = "sabnzbd"
    priority = 2
    supports_partial = False  # a usenet NZB is all-or-nothing

    async def search(self, artist: str, album: str) -> list[Candidate]:
        releases = await acquire.search(self.config, f"{artist} {album}", "music",
                                        ["prowlarr"])
        candidates = []
        for release in releases:
            # Prowlarr feeds both protocols; the torrent half is not ours
            if release["grab_ref"].get("engine") != "sabnzbd":
                continue
            title = release["title"]
            candidates.append(Candidate(
                channel=self.name,
                title=title,
                files=[CandidateFile(
                    name=title, size=release.get("size") or 0,
                    extension=_extension_from_categories(release.get("categories") or []),
                )],
                ref={"grab_ref": release["grab_ref"], "title": title},
                whole_album=True,
            ))
        return candidates

    async def inspect(self, candidate: Candidate) -> Contents:
        """What the post declares, read before committing to it. OPUS fetches
        and parses the NZB; what the names mean is decided here."""
        try:
            found = await acquire.inspect(self.config, candidate.ref["grab_ref"])
        except acquire.AcquireError:
            # nothing here may break a grab: a manifest that cannot be had
            # simply leaves the post judged by its title
            return Contents("unknown")
        if not found.get("available"):
            return Contents("unknown")
        return nzb.classify(found["files"])

    async def download(self, candidate: Candidate,
                       wanted_titles: list[str] | None = None) -> dict:
        # NZBs are all-or-nothing; a missing-only grab still fetches the album
        job_id = await acquire.grab(self.config, candidate.ref["grab_ref"], NAMESPACE)
        return {"job": job_id, "title": candidate.ref["title"]}

    async def job_status(self, job_ref: dict) -> JobStatus:
        status = await super().job_status(job_ref)
        if status.state != "unknown":
            status.files = [{"name": job_ref.get("title", ""), "state": status.state,
                             "progress": status.progress}]
        return status
