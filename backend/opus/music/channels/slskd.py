"""Soulseek channel — the search and the transfer happen in OPUS now.

The music half used to drive slskd itself: the search poll loop, the per-peer transfer
list, the state vocabulary. That is mechanism and it moved to OPUS · Downloads;
this channel is a client of `/search` + `/grab` + `/jobs`.

What stays here is judgement. A Soulseek response is one peer's folder, and the
things that decide whether it is worth taking — which files are audio, what
bitrate each one carries, whether the peer is fast and how many others are ahead
in its queue — are read here from what OPUS reports, and scored by
opus.music.matching. Soulseek is also the one channel that can fetch a subset, so a
missing-tracks grab hands OPUS a shortened file list."""

import posixpath

from opus import acquire
from opus.acquire import AcquireError
from opus.music.channels.base import Candidate, CandidateFile, Channel
from opus.music.tagging.tagger import AUDIO_EXTENSIONS
from opus.music.textnorm import title_score

NAMESPACE = "music"


class SlskdChannel(Channel):
    name = "slskd"
    priority = 1

    async def search(self, artist: str, album: str) -> list[Candidate]:
        # Soulseek answers for as long as peers answer, so this outlasts the
        # other channels' searches by design
        releases = await acquire.search(self.config, f"{artist} {album}", "music",
                                        [self.name], timeout=120)
        return [c for c in (self._to_candidate(r) for r in releases) if c]

    def _to_candidate(self, release: dict) -> Candidate | None:
        """One peer's folder. Only the audio in it is a candidate — a peer
        offering a folder of cue sheets and logs is offering nothing."""
        grab_ref = release["grab_ref"]
        audio = []
        for f in grab_ref.get("files", []):
            name = f["filename"].replace("\\", "/")
            if posixpath.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                audio.append(f)
        if not audio:
            return None
        return Candidate(
            channel=self.name,
            title=release["title"],
            files=[
                CandidateFile(
                    name=posixpath.basename(f["filename"].replace("\\", "/")),
                    size=f.get("size", 0),
                    extension=posixpath.splitext(f["filename"])[1].lower(),
                    bitrate=f.get("bitrate"),
                )
                for f in audio
            ],
            ref={"grab_ref": {**grab_ref, "files": audio}},
            speed_bps=release.get("speed_bps"),
            queue_length=release.get("queue_length"),
        )

    async def download(self, candidate: Candidate,
                       wanted_titles: list[str] | None = None) -> dict:
        grab_ref = dict(candidate.ref["grab_ref"])
        if wanted_titles:
            grab_ref["files"] = [
                f for f in grab_ref["files"]
                if any(title_score(title, posixpath.splitext(
                    posixpath.basename(f["filename"].replace("\\", "/")))[0]) >= 65
                    for title in wanted_titles)
            ]
            if not grab_ref["files"]:
                raise AcquireError("none of the wanted tracks are in this peer's folder")
        job_id = await acquire.grab(self.config, grab_ref, NAMESPACE)
        return {"job": job_id, "directory": grab_ref.get("directory", "")}
