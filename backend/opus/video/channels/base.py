"""Common download-channel contract. Grab channels (SABnzbd, qBittorrent)
take a release candidate found via Prowlarr; the ytdlp channel is
self-contained (no indexer). The pipeline is channel-agnostic."""

from dataclasses import dataclass, field

from opus import acquire
from opus.acquire import JobStatus


@dataclass
class Candidate:
    channel: str  # grab channel that can take this release
    title: str
    size: int
    protocol: str  # usenet | torrent
    seeders: int | None = None
    indexer: str = ""
    guid: str = ""
    ref: dict = field(default_factory=dict)
    score: float = 0.0
    parsed: dict = field(default_factory=dict)  # guessit output (resolution, source, ...)
    # what the engine already knew about the release
    age_days: float | None = None
    subs_hint: bool | None = None
    # what the release DECLARES it holds, once it has been inspected: the bytes
    # that are actually picture, and whether a file list was legible at all. The
    # advertised size is not the picture — a usenet release carries par2 recovery
    # worth up to a fifth of it.
    payload: int | None = None
    declared: str = ""


class Channel:
    name: str

    def __init__(self, config):
        self.config = config

    def grab_ref(self, candidate: Candidate) -> dict:
        return candidate.ref["grab_ref"]

    async def download(self, candidate: Candidate, category: str) -> dict:
        """Start the download tagged with the library's own category (isolation from
        other apps sharing the same worker); returns a job_ref dict for later
        status polling."""
        job_id = await acquire.grab(self.config, self.grab_ref(candidate), category)
        return {"job": job_id, "category": category, "title": candidate.title}

    async def job_status(self, job_ref: dict) -> JobStatus:
        return await acquire.job_status(self.config, job_ref)

    async def drop_job(self, job_ref: dict) -> None:
        await acquire.drop(self.config, job_ref)
