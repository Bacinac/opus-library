"""Common download-channel contract. Every channel (slskd, SABnzbd, and what a
plugin adds) implements this interface; the pipeline is channel-agnostic."""

import abc
from dataclasses import dataclass, field

from opus import acquire
from opus.acquire import JobStatus


@dataclass
class CandidateFile:
    name: str
    size: int
    extension: str
    bitrate: int | None = None


@dataclass
class Candidate:
    channel: str
    title: str
    files: list[CandidateFile]
    ref: dict = field(default_factory=dict)
    speed_bps: int | None = None
    queue_length: int | None = None
    # an all-or-nothing release (a usenet NZB) whose title names the whole album
    # — scored on album-title match + total size, not per-track file matching
    whole_album: bool = False


@dataclass(frozen=True)
class Contents:
    """What a release really holds, read before anything is downloaded. 'audio'
    carries the real file list; 'packed' (tracks sealed inside archives),
    'obfuscated' (every track posted under one name) and 'unknown' all mean the
    title is still all there is."""

    state: str  # audio | packed | obfuscated | unknown
    files: list[CandidateFile] = field(default_factory=list)


class Channel(abc.ABC):
    name: str
    priority: int
    # can the channel fetch a subset of an album's tracks? slskd (file-level)
    # and a store (track-level) can; a usenet NZB is all-or-nothing
    supports_partial: bool = True

    def __init__(self, config):
        self.config = config

    @classmethod
    def enabled(cls, config) -> bool:
        """Whether the installation takes part through this channel; a plugin's
        channel may be one it turns on only once an account is behind it."""
        return True

    @abc.abstractmethod
    async def search(self, artist: str, album: str) -> list[Candidate]: ...

    @abc.abstractmethod
    async def download(self, candidate: Candidate,
                       wanted_titles: list[str] | None = None) -> dict:
        """Start the download; returns a job_ref dict for later status polling.
        wanted_titles limits the grab to those songs on channels that can
        (slskd file subset, a store per track); others take the whole album."""

    async def inspect(self, candidate: Candidate) -> Contents:
        """Look inside the release before committing to it. A channel whose
        search already returns the real file list has nothing to add here."""
        return Contents("unknown")

    async def job_status(self, job_ref: dict) -> JobStatus:
        return await acquire.job_status(self.config, job_ref)

    async def drop_job(self, job_ref: dict) -> None:
        await acquire.drop(self.config, job_ref)
