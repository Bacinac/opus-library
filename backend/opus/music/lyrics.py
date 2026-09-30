"""The words to a song: where they are found, and what they look like once found.

Three places are asked, in the order of what they can give. A `.lrc` beside the
file and a SYLT/SYNCEDLYRICS tag inside it are the recording's own words WITH
timings. LRCLIB is a public archive keyed on artist, title, album and duration —
free, unauthenticated, and the only one of the three that has timings for most
of what is here. A plain LYRICS tag, which is what a rip usually carries, is
words without timings and therefore the last thing preferred, even though it
travelled with the file.

Asked when a record is opened or played and written down, rather than swept for
at import: a library of forty-eight thousand files would mean forty-eight
thousand requests to a free archive for words nobody has asked to read. A miss
is written down too — an instrumental is not a track to go back to the network
for on every play.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import mutagen
from mutagen.id3 import ID3
from mutagen.mp4 import MP4Tags
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opus.http import USER_AGENT
from opus.models import Track, TrackLyrics

log = logging.getLogger("opus.lyrics")

LRCLIB = "https://lrclib.net/api"

# how long a miss stands before the archive is worth asking again. Lyrics are
# added there by hand, so a song nobody has transcribed yet may be transcribed
# next month — but not next Tuesday, and not on every play
RETRY_MISS = timedelta(days=30)

# a cover, a live take and a remaster are all the same title by the same artist,
# and the length is what tells them apart
CLOSE_ENOUGH = 4

_STAMP = re.compile(r"\[(\d+):(\d{1,2}(?:[.:]\d{1,3})?)\]")

_SYNCED_TAGS = ("syncedlyrics", "lyrics_synced")
_PLAIN_TAGS = ("lyrics", "unsyncedlyrics", "unsynced lyrics")


def parse(lrc: str) -> list[dict]:
    """LRC as lines with a time each. A stamp may be repeated on one line — a
    chorus is written once and pointed at from every place it is sung — and a
    line with nothing after the stamp is the pause between verses, which is
    kept: it is what stops the previous line being highlighted for the whole of
    an instrumental break."""
    lines: list[dict] = []
    for raw in lrc.splitlines():
        stamps = list(_STAMP.finditer(raw))
        if not stamps:
            continue
        text = raw[stamps[-1].end():].strip()
        for stamp in stamps:
            seconds = int(stamp.group(1)) * 60 + float(stamp.group(2).replace(":", "."))
            lines.append({"at": round(seconds, 2), "text": text})
    lines.sort(key=lambda line: line["at"])
    return lines


def _timed(text: str | None) -> bool:
    return bool(text) and bool(_STAMP.search(text))


def _from_file(path: str) -> tuple[str | None, str | None]:
    """What the file itself carries. Tag dialects disagree on everything but the
    word: Vorbis comments spell it out, ID3 keeps it in USLT frames, MP4 in one
    atom — and a `lyricist` credit matches none of them, which is why keys are
    compared whole rather than searched for."""
    try:
        audio = mutagen.File(path)
    except Exception as exc:  # a truncated or foreign file is not a failure here
        log.debug("lyrics: %s could not be read: %s", path, exc)
        return None, None
    if audio is None or audio.tags is None:
        return None, None
    tags = audio.tags
    values: list[str] = []
    if isinstance(tags, ID3):
        values += [str(frame.text) for frame in tags.getall("USLT")]
        for frame in tags.getall("SYLT"):
            # SYLT is (text, milliseconds) pairs; written back out as LRC so
            # everything downstream reads one format
            pairs = getattr(frame, "text", None) or []
            written = [
                f"[{int(ms // 60000):02d}:{ms % 60000 / 1000:06.3f}]{chunk.strip()}"
                for chunk, ms in pairs
            ]
            if written:
                values.append("\n".join(written))
    elif isinstance(tags, MP4Tags):
        values += [str(v) for v in tags.get("\xa9lyr", [])]
    else:
        for key in (*_SYNCED_TAGS, *_PLAIN_TAGS):
            values += [str(v) for v in (tags.get(key) or [])]
    synced = next((v for v in values if _timed(v)), None)
    plain = next((v for v in values if v and not _timed(v)), None)
    return synced, plain


def _sidecar(path: str) -> str | None:
    beside = Path(path).with_suffix(".lrc")
    try:
        return beside.read_text(encoding="utf-8", errors="replace") if beside.is_file() else None
    except OSError:
        return None


def _closest(found: list[dict], seconds: int | None) -> dict | None:
    """The best of what a search turned up. Timings win over none of them, and
    within that the take whose length is nearest ours — a search for a song by
    its name answers with every version of it anybody has ever transcribed."""
    if not found:
        return None
    if seconds:
        found = [
            row for row in found
            if abs((row.get("duration") or 0) - seconds) <= CLOSE_ENOUGH
        ]
    if not found:
        return None
    found.sort(key=lambda row: (
        0 if row.get("syncedLyrics") else 1,
        abs((row.get("duration") or 0) - (seconds or 0)),
    ))
    return found[0]


async def _archive(artist: str, title: str, album: str, seconds: int | None) -> dict | None:
    """LRCLIB, name for name first. Its /get wants all four fields to agree, so
    a record filed here under a different edition title misses it entirely —
    which is what the search is for, and why the search result is then checked
    against the length rather than trusted.

    An answer from /get is taken and anything else is asked of the search,
    whose refusal is the refusal. It is not only 404s: the archive answers 503
    to some songs it then hands over through the search without complaint, and
    treating that as a refusal gave up on a song for no reason."""
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": USER_AGENT}) as http:
        resp = await http.get(f"{LRCLIB}/get", params={
            "artist_name": artist, "track_name": title,
            "album_name": album or "", "duration": int(seconds or 0),
        })
        if resp.status_code == 200:
            return resp.json()
        resp = await http.get(f"{LRCLIB}/search", params={
            "artist_name": artist, "track_name": title,
        })
        resp.raise_for_status()
        return _closest(resp.json(), seconds)


def _stale(row: TrackLyrics) -> bool:
    if row.source != "none":
        return False
    looked = row.looked_at
    if looked is None:
        return True
    if looked.tzinfo is None:
        looked = looked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - looked > RETRY_MISS


def _answer(row: TrackLyrics) -> dict:
    return {
        "source": row.source,
        "instrumental": row.instrumental,
        "lines": parse(row.synced) if row.synced else [],
        "plain": row.plain or "",
    }


class Unreachable(Exception):
    """The archive could not be asked. Kept apart from a song nobody has
    transcribed, because the two were written down the same way: one refused
    request marked a song wordless for the month RETRY_MISS lasts, and the
    screen said so in the same voice it says "instrumental"."""


async def _look(track: Track) -> dict:
    """Every place the words to one track can be, in order, without touching the
    session — so a whole record can be looked up at once and written down after."""
    path = track.files[0].path if track.files else None
    seconds = (track.files[0].duration_sec if track.files else None) or track.duration_sec

    synced, plain, source = await _on_disk(path) if path else (None, None, "none")
    instrumental = False
    # the archive is asked whenever the file had no timings, even when it had
    # the words: a line that lights up as it is sung is a different thing from a
    # page of text, and it is the reason this screen exists
    if not synced:
        got = await _ask_archive(track, seconds, source)
        if got:
            synced, plain, source, instrumental = _with_archive(got, synced, plain, source)

    if plain is None and synced:
        # one page of words, out of the timed ones: what a screen too small for
        # a scrolling column shows, and what is left when nothing else has it
        plain = "\n".join(dict.fromkeys(line["text"] for line in parse(synced) if line["text"]))

    return {"synced": synced, "plain": plain, "source": source, "instrumental": instrumental}


async def _on_disk(path: str) -> tuple[str | None, str | None, str]:
    synced, plain = await asyncio.to_thread(_from_file, path)
    source = "file" if synced or plain else "none"
    beside = await asyncio.to_thread(_sidecar, path)
    if beside and _timed(beside):
        synced, source = beside, "sidecar"
    return synced, plain, source


async def _ask_archive(track: Track, seconds: int | None, source: str) -> dict | None:
    try:
        return await _archive(track.release.artist.name, track.title,
                              track.release.title, seconds)
    except httpx.HTTPError as exc:
        log.warning("lyrics: lrclib refused %r by %r: %s", track.title,
                    track.release.artist.name, exc)
        # what the file itself carried is still the words to this song; it is
        # only the timed ones that were not had
        if source == "none":
            raise Unreachable(str(exc)) from exc
        return None


def _with_archive(got: dict, synced: str | None, plain: str | None,
                  source: str) -> tuple[str | None, str | None, str, bool]:
    instrumental = bool(got.get("instrumental"))
    if got.get("syncedLyrics"):
        synced, source = got["syncedLyrics"], "lrclib"
    if got.get("plainLyrics") and not plain:
        plain = got["plainLyrics"]
        if source == "none":
            source = "lrclib"
    if instrumental and source == "none":
        source = "lrclib"
    return synced, plain, source, instrumental


def _put(session: AsyncSession, kept: TrackLyrics | None, track_id: int,
         found: dict) -> TrackLyrics:
    row = kept or TrackLyrics(track_id=track_id)
    row.synced, row.plain = found["synced"], found["plain"]
    row.instrumental, row.source = found["instrumental"], found["source"]
    row.looked_at = datetime.now(timezone.utc)
    if kept is None:
        session.add(row)
    return row


async def words(session: AsyncSession, track: Track, refresh: bool = False) -> dict:
    """The words to one track, from wherever they can be had, remembered."""
    kept = await session.get(TrackLyrics, track.id)
    if kept is not None and not refresh and not _stale(kept):
        return _answer(kept)
    found = await _look(track)
    row = _put(session, kept, track.id, found)
    await session.commit()
    return _answer(row)


def _has(row: TrackLyrics) -> bool:
    return bool(row.synced or row.plain)


# how many of a record's songs are looked up at once. The archive is free and
# unauthenticated, and a record is twelve songs: three at a time finishes while
# somebody is still reading the sleeve, and nobody there notices us at all.
LOOKERS = 3


async def album(session: AsyncSession, tracks: list[Track]) -> dict:
    """Which songs on a record have words. Asked for the whole record at once
    because the mark beside a song is drawn before anybody presses it — and once
    asked it is written down, so putting the record on costs nothing later.

    A song the archive could not be asked about is neither: it is named, so the
    screen can leave it unmarked rather than call it wordless."""
    keep: dict[int, TrackLyrics] = {
        row.track_id: row
        for row in (
            await session.execute(
                select(TrackLyrics).where(TrackLyrics.track_id.in_([t.id for t in tracks])))
        ).scalars()
    }
    ask = [t for t in tracks if t.id not in keep or _stale(keep[t.id])]
    gate = asyncio.Semaphore(LOOKERS)

    async def one(track: Track) -> tuple[Track, dict | None]:
        async with gate:
            try:
                return track, await _look(track)
            except Unreachable:
                return track, None

    for track, found in await asyncio.gather(*(one(t) for t in ask)):
        if found is not None:
            keep[track.id] = _put(session, keep.get(track.id), track.id, found)
    if ask:
        await session.commit()

    return {
        "words": [t.id for t in tracks if t.id in keep and _has(keep[t.id])],
        "unknown": [t.id for t in tracks if t.id not in keep],
    }
