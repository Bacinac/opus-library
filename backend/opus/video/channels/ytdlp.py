"""Web video — the fetch happens in OPUS now.

The download itself moved: OPUS runs yt-dlp inside its own process, and a
restart there fails the job loudly rather than losing it — the same promise
this channel used to make for itself, now made once for every module.

What did NOT move is knowing what is on the web. `opus.video.metadata.webvideo`
probes a URL and enumerates a channel's uploads, because that is catalogue
knowledge and catalogue knowledge is the library's — exactly as a catalogue's
*account* stays with the music module while its downloading left.

Nor did the policy. Which format to take, which subtitle languages the
acceptance gate will demand, whether a machine transcript counts, whether
sponsor segments are cut: all four ride in the grab_ref, because they are
decisions OPUS cannot make and must not invent."""

from opus.video.channels.base import Candidate, Channel


class YtdlpChannel(Channel):
    name = "ytdlp"

    def grab_ref(self, candidate: Candidate) -> dict:
        return {
            "engine": self.name,
            "url": candidate.ref["url"],
            "title": candidate.title,
            "format": self.config.get("ytdlp_format"),
            # the gate downstream judges the subtitles this asks for, so asking
            # for none here would fail every item it ever fetched
            "subtitles": self.config.langs(),
            "auto_subtitles": self.config.bool("accept_auto_subs"),
            "sponsorblock": self.config.bool("sponsorblock"),
        }
