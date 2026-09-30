"""Which release on which channel: asking the channels, scoring what they
offer, and confirming the leader before anything is downloaded."""

import logging
from dataclasses import dataclass, replace

from sqlalchemy import func, select

from opus.music.channels import enabled_channels
from opus.acquire import AcquireError
from opus.music.channels.base import Channel, Contents
from opus.db import SessionLocal
from opus.music.matching import quality
from opus.music.matching.engine import (ScoredCandidate, apply_quality_ceiling,
                                        apply_quality_profile, score_candidates)
from opus.music.metadata import tracklists
from opus.models import (DEAD_SOURCE, DOWNLOAD_SPENT, MusicDownload, MusicDownloadStatus, Release,
                         ReleaseStatus)
from opus.settings_store import RuntimeConfig, current_runtime
from opus.music.pipeline import state

log = logging.getLogger("opus.music.pipeline")


# fetching a manifest may count against the indexer's daily grab quota, so only
# the leading candidates are opened; the rest keep the judgement their title
# earned them
MANIFEST_PEEKS = 2


async def search_candidates(release_id: int) -> list[ScoredCandidate] | None:
    """Every candidate any enabled channel finds for this release, scored but
    UNFILTERED — nothing dropped for a zero match, a quality ceiling or profile,
    or a missing surround/DSD word in its title. Read-only: no status write, no
    NZB history, nothing recorded. The automatic grab already had its turn on
    this album; this is the human's, to see what is actually out there —
    including a post the matcher could never have taken on its own, a
    compilation whose title does not begin with an artist the catalog knows —
    and choose one by hand.

    A tracklist the catalog cannot supply is not a reason to refuse the list:
    an unmatched title still scores on its size and its stated format, and
    still shows up, low but present, for a human to recognise on sight."""
    async with SessionLocal() as session:
        release = await session.get(Release, release_id)
        if release is None:
            return None
        artist = await release.awaitable_attrs.artist
        try:
            tracks = await tracklists.ensure_tracks(session, release)
        except tracklists.DiscographyError:
            tracks = []
        await session.commit()
        artist_name, album_title = artist.name, release.title
        track_titles = [t.title for t in tracks]
        config = await current_runtime()

    scored: list[ScoredCandidate] = []
    for channel in channels_to_ask(config, automatic=False):
        try:
            candidates = await channel.search(artist_name, album_title)
        except AcquireError as exc:
            log.error("candidates for release %s: %s search failed: %s",
                     release_id, channel.name, exc)
            continue
        scored.extend(score_candidates(candidates, track_titles, album_title,
                                       artist_name, drop_unmatched=False))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


# What a post says when it is the other edition. A surround mix is announced in
# the title or not at all — nobody presses a 5.1 disc quietly — so this is the
# whole of what there is to go on before anything is downloaded.
SURROUND_SAID = (
    "5.1", "5_1", "7.1", "quad", "quadraphonic", "sacd", "dvd-audio", "dvda",
    "dvd audio", "blu-ray audio", "bluray audio", "atmos", "multichannel",
    "multi-channel", "surround",
)


def _surround_only(scored: list, wanted: bool) -> list:
    """When the surround edition is what was asked for, only a post that says so
    will do. The stereo master is already on the shelf, and fetching it again is
    fetching nothing — better to find none and say so."""
    if not wanted:
        return scored
    said = [s for s in scored
            if any(word in s.candidate.title.lower() for word in SURROUND_SAID)]
    if not said and scored:
        log.info("nothing among %d posts says it is a surround edition", len(scored))
    return said


def _dsd_only(scored: list, wanted: bool) -> list:
    """When the DSD edition is what was asked for, only a post the title itself
    proves is DSD will do — the same reading `quality.parse_title` gives every
    candidate already, so a 'DSD64' or a bare 'SACD-R'/'SACD ISO' both count and
    a post merely mentioning a lossless claim does not."""
    if not wanted:
        return scored
    said = [s for s in scored if quality.parse_title(s.candidate.title).codec == "dsd"]
    if not said and scored:
        log.info("nothing among %d posts says it is a DSD edition", len(scored))
    return said


async def _confirmed_best(release_id: int, channel, viable, track_titles: list[str],
                          album_title: str, artist_name: str, config):
    """The leading candidate, judged wherever possible on what the release
    actually holds rather than on what its title claims. A usenet post that is
    not packed lists its real filenames, so completeness stops being a reading
    of the title and the format stops being a guess: a post that carries a
    different album, or a resolution above the ceiling, is dropped here instead
    of after a wasted download. It is also the only thing that can speak for a
    box set, whose title says nothing about which albums are inside — one that
    cannot be read is left where it was rather than taken on faith."""
    # one post reaches us from several indexers under the same title; opening it
    # once is both the quota-cheap and the consistent thing to do
    opened: dict[str, Contents] = {}
    for scored in viable:
        title = scored.candidate.title
        if title not in opened and len(opened) >= MANIFEST_PEEKS:
            unread = "the manifest budget is spent"
        else:
            if title not in opened:
                opened[title] = await channel.inspect(scored.candidate)
            contents = opened[title]
            # one audio file for a multi-track album is a whole-album image (a
            # single FLAC beside a cue sheet): it carries the album's name, not
            # the tracks', and holding it against a tracklist would throw a good
            # post away
            image = len(contents.files) == 1 and len(track_titles) > 1
            unread = (
                "" if contents.state == "audio" and not image
                else "it is a whole-album image" if image
                # the channel holds no manifest to read (slskd already returns
                # its real files), or the fetch failed and said so where it
                # happened
                else "there is no file list to read" if contents.state == "unknown"
                else f"its manifest is {contents.state}")
        if unread:
            if scored.unproven:
                log.info("release %s: leaving %r, it carries more than this "
                         "album and %s", release_id, title, unread)
                continue
            log.info("release %s: taking %r on its title, %s",
                     release_id, title, unread)
            return scored
        revised = replace(scored.candidate, files=contents.files, whole_album=False)
        measured = score_candidates([revised], track_titles, album_title, artist_name)
        confirmed = apply_quality_ceiling(measured, config.get("max_quality"))
        # a profile is a preference across the field, not a property of one
        # post, so the revealed quality goes back among its rivals: an MP3 that
        # passed for lossless on its title loses to any post still claiming it
        field = sorted(
            apply_quality_profile(
                confirmed + [s for s in viable if s is not scored],
                config.get("music_quality_profile")),
            key=lambda s: s.score, reverse=True)
        if (confirmed and field and field[0] is confirmed[0]
                and confirmed[0].score >= config.float("min_candidate_score")):
            log.info("release %s: %r confirmed by its own manifest — %d audio "
                     "files, completeness %.2f, %s", release_id, title,
                     len(contents.files), confirmed[0].completeness,
                     confirmed[0].quality.codec or "codec unknown")
            return confirmed[0]
        detail = (f"completeness {measured[0].completeness:.2f}, "
                  f"{measured[0].quality.codec or 'codec unknown'}" if measured
                  else "holds none of the tracklist")
        log.info("release %s: %r rejected by its own manifest — %d audio files, %s",
                 release_id, title, len(contents.files), detail)
    return None


@dataclass(frozen=True)
class _Brief:
    artist_name: str
    album_title: str
    track_titles: list[str]
    config: RuntimeConfig
    failed_channels: set[str]
    tried_nzb_titles: set[str]
    download_count: int


async def brief(release_id: int, automatic: bool) -> _Brief | None:
    async with SessionLocal() as session:
        release = await session.get(Release, release_id)
        if release is None:
            log.error("grab_release: release %s not found", release_id)
            return None
        artist = await release.awaitable_attrs.artist
        release.status = ReleaseStatus.SEARCHING
        await session.commit()

        try:
            tracks = await tracklists.ensure_tracks(session, release)
        except tracklists.DiscographyError as exc:
            release.status = ReleaseStatus.FAILED
            await session.commit()
            log.error("release %s: %s", release_id, exc)
            return None
        await session.commit()

        return _Brief(
            artist_name=artist.name, album_title=release.title,
            track_titles=[t.title for t in tracks],
            config=await current_runtime(),
            failed_channels=await _failed_channels(session, release_id),
            tried_nzb_titles=await _tried_nzb_titles(session, release_id, automatic),
            download_count=(await session.execute(
                select(func.count()).select_from(MusicDownload)
                .where(MusicDownload.release_id == release_id)
            )).scalar() or 0,
        )


async def _failed_channels(session, release_id: int) -> set[str]:
    # a channel whose earlier download for THIS release failed (e.g. a store
    # has only lossy AAC for the album) is skipped so the grab falls
    # through to the next source instead of retrying the dead one
    failed = {
        row[0] for row in (await session.execute(
            select(MusicDownload.channel).where(
                MusicDownload.release_id == release_id,
                MusicDownload.status.in_(DEAD_SOURCE),
            )
        )).all()
    }
    # usenet is retried with a DIFFERENT NZB (see _tried_nzb_titles), never
    # blacklisted wholesale — one bad NZB does not rule out the channel
    failed.discard("sabnzbd")
    return failed


async def _tried_nzb_titles(session, release_id: int, automatic: bool) -> set[str]:
    # only the automatic recovery must reach for a DIFFERENT post: the one
    # already pulled gave everything it holds. A user-initiated grab keeps
    # the best post eligible — excluding it there locked the only usenet
    # source for an album out for life.
    # a post that FAILED is never worth pulling again, whoever asked: one
    # scans-only post was re-downloaded eight times, and an unrepairable
    # one cannot become repairable. A post that SUCCEEDED is excluded only
    # for the automatic recovery, which needs a different edition; a
    # user-initiated grab may reach for the best post again.
    statuses = (list(DOWNLOAD_SPENT) if not automatic
                else list(MusicDownloadStatus))
    tried = {
        (row[0] or {}).get("title")
        for row in (await session.execute(
            select(MusicDownload.job_ref).where(
                MusicDownload.release_id == release_id,
                MusicDownload.channel == "sabnzbd",
                MusicDownload.status.in_(statuses),
            )
        )).all()
    }
    tried.discard(None)
    return tried


def channels_to_ask(config: RuntimeConfig, automatic: bool) -> list[Channel]:
    channels = enabled_channels(config)
    if automatic:
        # a fresh complete usenet album beats patching single tracks from slskd
        # (usenet is fast); untried whole-album NZBs first, then partial channels
        channels = ([c for c in channels if not c.supports_partial]
                    + [c for c in channels if c.supports_partial])
    return channels


async def best_on_channel(release_id: int, channel: Channel, brief: _Brief,
                           mode: str) -> ScoredCandidate | None:
    state.searching[release_id] = channel.name
    try:
        candidates = await channel.search(brief.artist_name, brief.album_title)
    except AcquireError as exc:
        log.error("channel %s search failed: %s", channel.name, exc)
        return None
    finally:
        state.searching.pop(release_id, None)

    # never re-pull an NZB already tried for this release — a re-grab must
    # reach for a different whole-album post
    candidates = [c for c in candidates
                  if not (channel.name == "sabnzbd"
                          and c.ref.get("title") in brief.tried_nzb_titles)]
    viable = _viable(candidates, brief, mode)
    if not viable:
        log.info("channel %s: %d candidates, none viable", channel.name, len(candidates))
        return None
    best = await _confirmed_best(release_id, channel, viable, brief.track_titles,
                                 brief.album_title, brief.artist_name, brief.config)
    if best is None:
        log.info("channel %s: %d candidates, none held the album",
                 channel.name, len(candidates))
    return best


def _viable(candidates: list, brief: _Brief, mode: str) -> list[ScoredCandidate]:
    config = brief.config
    scored = score_candidates(candidates, brief.track_titles, brief.album_title,
                              brief.artist_name)
    viable = [s for s in scored if s.score >= config.float("min_candidate_score")]
    if mode != "dsd":
        # a PCM ceiling has no rung for a 1-bit stream to sit under, so
        # exceeds_ceiling refuses DSD outright below "any" — asking for the
        # DSD edition by name already says the ceiling does not apply here
        viable = apply_quality_ceiling(viable, config.get("max_quality"))
    viable = apply_quality_profile(viable, config.get("music_quality_profile"))
    viable = _surround_only(viable, mode == "surround")
    return _dsd_only(viable, mode == "dsd")
