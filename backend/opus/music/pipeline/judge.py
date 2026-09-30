"""Whether what arrived is what was wanted: the edition, the channel layout
and the lossless claim."""

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from opus.db import SessionLocal
from opus.music.library import editions
from opus.music.matching import quality
from opus.music.metadata.discogs import DiscogsClient
from opus.models import MusicFile, Release, Track
from opus.music.tagging import tagger
from opus.music.pipeline.arrival import Arrival
from opus.music.pipeline import grab

log = logging.getLogger("opus.music.pipeline")


# what a channel count is called; anything else is named by its count
_LAYOUT_NAMES = {1: "mono", 2: "stereo", 6: "5.1", 7: "6.1", 8: "7.1"}

# fake-FLAC guard: a genuine 44.1kHz lossless track carries real energy up to
# ~22kHz, while an MP3-sourced "FLAC" has a hard wall near 20kHz, so its >20kHz
# band is near silence. Genuine tracks measured at -24..-44 dBFS here, so -75 is
# a wide, false-positive-safe threshold that still catches blatant transcodes.
_HF_CUTOFF_HZ = 20000
_TRANSCODE_PEAK_DBFS = -80.0
_PEAK_RE = re.compile(r"Peak level dB:\s*(-?(?:inf|\d+(?:\.\d+)?))")


def _hf_peak_dbfs(path: Path) -> float | None:
    """Peak level (dBFS) of the >20kHz band over a mid-file sample, or None if it
    can't be measured (then the caller does not reject)."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", "20", "-t", "40",
             "-i", str(path),
             "-af", f"highpass=f={_HF_CUTOFF_HZ},astats=metadata=1:reset=0",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=90,
            # ffmpeg echoes the file's own metadata, which carries whatever
            # encoding the tagger used — strict UTF-8 decoding raised on a
            # legacy-encoded tag and killed the whole import
            encoding="utf-8", errors="replace",
        )
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        log.warning("HF analysis failed for %s: %s", path.name, exc)
        return None
    peaks = [float("-inf") if "inf" in m else float(m)
             for m in _PEAK_RE.findall(proc.stderr)]
    return max(peaks) if peaks else None


def _looks_transcoded(path: Path) -> bool:
    peak = _hf_peak_dbfs(path)
    if peak is None:
        return False
    if peak < _TRANSCODE_PEAK_DBFS:
        log.warning("track %s looks transcoded: >%dkHz peak %.1f dBFS",
                    path.name, _HF_CUTOFF_HZ // 1000, peak)
        return True
    return False


async def _held_layouts(release_id: int) -> set[int]:
    """The channel layouts the release's files already carry. A file probed
    before the layout was read states nothing and is evidence of neither
    agreement nor disagreement."""
    async with SessionLocal() as session:
        held = (await session.execute(
            select(MusicFile.channels).distinct()
            .join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == release_id,
                   MusicFile.channels.is_not(None))
        )).scalars().all()
    return set(held)


def _layouts(channels: set[int]) -> str:
    return " and ".join(_LAYOUT_NAMES.get(c, f"{c}-channel")
                        for c in sorted(channels))


async def _confirm_downloaded_edition(release_id: int, artist_name: str,
                                      album_title: str, plan, config) -> str | None:
    """Whether the download is a complete edition of this album in its own
    right — a different pressing with a different tracklist — rather than a
    short delivery of the catalog's. Only asked when the release holds nothing
    yet: adopting an edition rewrites the tracklist, which must not happen
    under files that are already there."""
    async with SessionLocal() as session:
        held = (await session.execute(
            select(func.count()).select_from(MusicFile)
            .join(Track, Track.id == MusicFile.track_id)
            .where(Track.release_id == release_id)
        )).scalar()
        if held:
            return None
        release = await session.get(Release, release_id)
        releases = (await session.execute(
            select(Release).where(Release.artist_id == release.artist_id)
        )).scalars().all()
        discogs = (DiscogsClient(config.get("discogs_token"))
                   if config.get("discogs_token") else None)
        try:
            # the question is whether ONE edition explains every file that was
            # downloaded — not whether it explains the catalog's tracklist
            return await editions.confirm_edition(
                session, discogs, release, releases, artist_name, album_title,
                len(plan.audio_files), plan.rows)
        except Exception:
            log.exception("edition check failed for release %s", release_id)
            return None
        finally:
            if discogs is not None:
                await discogs.close()


async def judge_import(arrival: Arrival, plan) -> tuple[bool, object]:
    """Whether the download may be imported at all, and the edition it turned
    out to be when it is not the catalog's. A refusal is settled here."""
    download_id, release_id = arrival.download_id, arrival.release_id
    # only the audio this download would actually put in the library is judged:
    # a box set's other albums are not this one's business, and the transcode
    # analysis over all of them would cost hours
    delivered = (sorted(plan.pairs.values()) if arrival.multi_album
                 else list(range(len(plan.audio_files))))
    disagreement = await _mix_disagreement(arrival, plan, delivered)
    if disagreement:
        await grab.reject(download_id,
                      f"one album is one mix: this download is {disagreement}")
        log.info("release %s: download from %s refused, it is %s",
                 release_id, arrival.channel_name, disagreement)
        return False, None

    if arrival.config.get("music_quality_profile") == "lossless_only":
        reasons = await _lossy_reasons([plan.audio_files[i] for i in delivered])
        if reasons:
            await grab.reject(download_id,
                          "lossless_only profile, but " + "; ".join(reasons))
            return False, None

    # what this download would deliver, decided before a file moves
    track_refs = arrival.track_refs
    if plan.matched >= len(track_refs):
        return True, None
    short = (f"{len(track_refs) - plan.matched} of {len(track_refs)} "
             f"tracks unfilled")
    if arrival.mode == "replace":
        # a replacement must deliver the WHOLE album from this one source;
        # anything less would patch a second edition over the first
        await grab.reject(download_id, f"would leave {short} — not a replacement")
        log.info("release %s: replacement from %s rejected, %d/%d tracks",
                 release_id, arrival.channel_name, plan.matched, len(track_refs))
        return False, None
    if arrival.multi_album:
        # the post was taken because its file list proved it carries this
        # album. It does not, so the proof was wrong — and a box's other
        # albums must never be half-imported as this one, nor its file
        # count read as an edition of it
        await grab.reject(download_id,
                      f"carries other albums and would leave {short}")
        log.info("release %s: multi-album post from %s discarded, %d/%d "
                 "tracks — it does not hold this album", release_id,
                 arrival.channel_name, plan.matched, len(track_refs))
        return False, None
    return True, await _confirm_downloaded_edition(
        release_id, arrival.artist_name, arrival.album_title, plan, arrival.config)


async def _mix_disagreement(arrival: Arrival, plan, delivered: list[int]) -> str | None:
    """A 5.1 or an Atmos rip is a different RECORD from the stereo one, never a
    better copy of it. A set that disagrees with ITSELF is still two records the
    library would stop being able to tell apart, and still refused. But
    disagreeing with what is already filed is the whole point when the surround
    or the DSD edition is what was asked for — each is meant to stand beside
    the stereo one, in its own folder, under the same album. A layout the files
    do not state stays unknown and rules nothing out.

    DSD carries its own second check: the title is what said this post was DSD
    — nothing about a manifest proves it — so a post that turns out to deliver
    plain FLAC under a DSD-sounding title is caught here rather than filed as
    if it were the edition asked for."""
    layouts = {plan.rows[i].channels for i in delivered if plan.rows[i].channels}
    held = await _held_layouts(arrival.release_id)
    wanted_surround = arrival.mode == "surround"
    wanted_dsd = arrival.mode == "dsd"
    if len(layouts) > 1:
        return _layouts(layouts)
    if wanted_surround and layouts and max(layouts) <= 2:
        return f"{_layouts(layouts)}, and a surround edition was asked for"
    if wanted_dsd:
        if layouts and max(layouts) > 2:
            return f"{_layouts(layouts)}, and a DSD edition — stereo only — was asked for"
        not_dsd = sorted({plan.rows[i].codec for i in delivered} - tagger.DSD_CODECS - {None})
        if not_dsd:
            return f"{', '.join(not_dsd)}, and a DSD edition was asked for"
    if held and not layouts <= held and not wanted_surround and not wanted_dsd:
        return f"{_layouts(layouts)} but the release holds {_layouts(held)}"
    return None


async def _lossy_reasons(guarded: list[Path]) -> list[str]:
    lossy = [p.name for p in guarded
             if not quality.is_lossless(quality.from_file(p.suffix))]
    faked = []
    for p in guarded:
        if p.suffix.lower() == ".flac" and await asyncio.to_thread(_looks_transcoded, p):
            faked.append(p.name)
    reasons = []
    if lossy:
        reasons.append(f"lossy audio: {', '.join(lossy[:3])}")
    if faked:
        reasons.append(f"FLAC transcoded from a lossy source: {', '.join(faked[:3])}")
    return reasons
