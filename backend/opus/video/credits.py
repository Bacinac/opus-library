"""Where an episode's closing credits begin, for the button that goes on to the
next one, and where its opening titles are, for the button that skips them.

Three witnesses, believed in this order. The file's own chapter, when it names
the credits. The picture going over to text on black and staying there to the
end. And the sound an episode shares with the rest of its season, which is its
closing theme. Chapters name the credits in fewer than a quarter of the
episodes here, the picture misses credits drawn over anything but black, and
the sound misses a series that closes every episode on a different song.

The sound is asked only where the picture found nothing. Where both answer and
differ, the picture was the one that was right: The Rings of Power opens its
credits on music of each episode's own, and the shared theme comes two minutes
later.

The opening titles have two witnesses: the file's own chapter, and the longest
stretch of sound an episode shares with others of its season near its start.
Where that stretch sits is not assumed. Bosch plays up to fifteen minutes of
story before its titles, and a different length in every episode, so the
comparison is at any offset — the story before the titles and the "previously"
are each episode's own, and only the titles are shared.

Without that sound there is no button, whatever the chapters say. A file can
carry the chapter table of another cut of its episode: The White Lotus's run
half a minute off, and Mrs. America has one table for all its episodes. A table
that puts the titles where the sound does not hear them, or that runs past the
end of the file, is believed about neither the titles nor the credits."""

import asyncio
import datetime
import logging
import os
import re
import statistics
from array import array
from itertools import combinations

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.models import Episode, Season, VideoFile

log = logging.getLogger(__name__)

# how much of the end is looked at: the longest credits here run six minutes
TAIL_S = 600.0
CHAPTER = re.compile(r"^(end |closing )?credits$|^end titles$|^outro$|^ending$", re.IGNORECASE)
# a chapter that far from the end is not the closing credits, whatever it is called
CHAPTER_WITHIN = 0.25

# the share of a frame below black's threshold, as ffmpeg's blackframe counts it.
# Credits begin on a frame this dark and stay above STILL_DARK to the end, with
# no lit stretch longer than GAP_S: a final scene can be as dark as the credits
# for a minute, but it does not run on into them unbroken
DARK = 95
STILL_DARK = 80
RUN_S = 15.0
GAP_S = 4.0
# a logo or a scene after the credits
END_SLACK_S = 60.0

FP_ITEM_S = 0.1238
FP_SAME_BITS = 8
FP_GAP = int(3.0 / FP_ITEM_S)
FP_RUN = int(12.0 / FP_ITEM_S)
SEASON_PEERS = 8

# how much of the start is listened to, and never into the stretch the closing
# credits are looked for in: a sitcom's closing theme is longer than its titles
OPEN_S = 1200.0
INTRO_CHAPTER = re.compile(
    r"^(\d+\.\s*)?(intro|opening( credits| titles| theme| sequence)?|title sequence|main titles?|vorspann)$",
    re.IGNORECASE)
# a title card under this is over before a button could be reached, and a shared
# stretch over this is the same episode twice, or a chapter wrongly placed
INTRO_SHORTEST_S = 15.0
INTRO_LONGEST_S = 180.0
# a file's own chapter begins its titles within six seconds of the sound, and a
# borrowed table eleven or more away
CHAPTER_AGREES_S = 10.0
# how many of a second's fingerprints must agree for that second to be shared.
# The gap a run bridges lets a stray agreement three seconds out stretch it, and
# at the edge of a title card that is story: The Good Wife's nine seconds of
# titles measured nineteen
FP_SECOND = round(1 / FP_ITEM_S)
FP_DENSE = 3

IDLE_S = 3600
_LINE = re.compile(r"pblack:(\d+) pts:\S+ t:([\d.]+)")


async def _run(*args: str, text: bool = True) -> str | bytes:
    proc = await asyncio.create_subprocess_exec(
        "nice", "-n", "19", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if text:
        return err.decode(errors="replace")
    return out


def _table(chapters, duration: float) -> list:
    ordered = sorted(chapters, key=lambda c: c.start_s)
    return ordered if ordered and ordered[-1].start_s < duration else []


def from_chapters(chapters, duration: float) -> float | None:
    for chapter in _table(chapters, duration):
        if CHAPTER.match(chapter.title.strip()) and chapter.start_s >= duration * (1 - CHAPTER_WITHIN):
            return chapter.start_s
    return None


async def _darkness(path: str, start: float, length: float | None = None,
                    keys: bool = True, fps: int | None = None) -> list[tuple[float, int]]:
    args = ["ffmpeg", "-hide_banner", "-nostdin"]
    if keys:
        args += ["-skip_frame", "nokey"]
    args += ["-ss", f"{start:.2f}"]
    if length:
        args += ["-t", f"{length:.2f}"]
    scale = (f"fps={fps}," if fps else "") + "scale=160:-2,blackframe=amount=0:threshold=32"
    said = await _run(*args, "-i", path, "-an", "-sn", "-dn", "-vf", scale, "-f", "null", "-")
    return [(start + float(t), int(p)) for p, t in _LINE.findall(said)]


async def from_picture(path: str, duration: float) -> float | None:
    """The first stretch of text on black that runs on to the end. Keyframes
    find it cheaply; the frames just before the first dark keyframe say to the
    fraction of a second where it began."""
    start = max(0.0, duration - TAIL_S)
    keys = await _darkness(path, start)
    if len(keys) < 5:
        return None
    end = keys[-1][0]
    for i, (at, share) in enumerate(keys):
        if share < DARK:
            continue
        last = at
        for t, s in keys[i + 1:]:
            if s >= STILL_DARK:
                last = t
            elif t - last > GAP_S:
                break
        if last - at < RUN_S or end - last > END_SLACK_S:
            continue
        before = keys[i - 1][0] if i else start
        fine = await _darkness(path, before, at - before + 0.5, keys=False, fps=8)
        return next((t for t, s in fine if s >= DARK), at)
    return None


async def _fingerprint(path: str, duration: float) -> tuple[float, array]:
    start = max(0.0, duration - TAIL_S)
    raw = await _run("ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.2f}", "-i", path,
                     "-vn", "-ac", "1", "-f", "chromaprint", "-fp_format", "raw", "-", text=False)
    prints = array("I")
    prints.frombytes(raw[: len(raw) // 4 * 4])
    return start, prints


def opening_window(duration: float) -> float:
    return max(0.0, min(OPEN_S, duration - TAIL_S))


async def _opening_print(path: str, duration: float) -> array:
    raw = await _run("ffmpeg", "-v", "error", "-nostdin", "-t", f"{opening_window(duration):.2f}",
                     "-i", path, "-vn", "-ac", "1", "-f", "chromaprint", "-fp_format", "raw", "-",
                     text=False)
    prints = array("I")
    prints.frombytes(raw[: len(raw) // 4 * 4])
    return prints


def _shared(a: array, b: array) -> tuple[int, int, int]:
    """(where in a, where in b, how long) of the longest stretch of sound the
    two have in common. Identical sound repeats exact fingerprint values, which
    says how the two are shifted; the values either side of those confirm it."""
    where_b: dict[int, list[int]] = {}
    for j, value in enumerate(b):
        where_b.setdefault(value, []).append(j)
    shifts: dict[int, int] = {}
    for i, value in enumerate(a):
        for j in where_b.get(value, ())[:8]:
            shifts[i - j] = shifts.get(i - j, 0) + 1
    best = (0, 0, 0)
    for shift, _ in sorted(shifts.items(), key=lambda kv: -kv[1])[:12]:
        lo_a = max(0, shift)
        lo_b = lo_a - shift
        n = min(len(a) - lo_a, len(b) - lo_b)
        run_start = last = None
        for k in range(n):
            if (a[lo_a + k] ^ b[lo_b + k]).bit_count() > FP_SAME_BITS:
                continue
            if run_start is None or k - last > FP_GAP:
                run_start = k
            last = k
            if last - run_start > best[2]:
                best = (lo_a + run_start, lo_b + run_start, last - run_start)
    return best


def from_sound(prints: dict[int, tuple[float, array]]) -> dict[int, float]:
    """For every file of a season, where the sound it shares with the others
    begins — the median over every other episode it was compared with, so one
    recurring cue in two episodes does not decide it."""
    found: dict[int, list[float]] = {key: [] for key in prints}
    for (ka, (sa, fa)), (kb, (sb, fb)) in combinations(prints.items(), 2):
        ia, ib, n = _shared(fa, fb)
        if n >= FP_RUN:
            found[ka].append(sa + ia * FP_ITEM_S)
            found[kb].append(sb + ib * FP_ITEM_S)
    return {key: statistics.median(at) for key, at in found.items() if at}


def intro_chapters(chapters, duration: float) -> list[tuple[float, float]]:
    ordered = _table(chapters, duration)
    return [(here.start_s, after.start_s) for here, after in zip(ordered, ordered[1:])
            if INTRO_CHAPTER.match(here.title.strip())
            and here.start_s < opening_window(duration)
            and INTRO_SHORTEST_S <= after.start_s - here.start_s <= INTRO_LONGEST_S]


def _dense(a: array, b: array, ia: int, ib: int, n: int) -> tuple[int, int]:
    """The run cut to where it is shared second by second, as offsets into it."""
    same = [(a[ia + k] ^ b[ib + k]).bit_count() <= FP_SAME_BITS for k in range(n)]

    def shared_from(k: int) -> bool:
        return same[k] and sum(same[k:k + FP_SECOND]) >= FP_DENSE

    def shared_to(k: int) -> bool:
        return same[k] and sum(same[max(0, k - FP_SECOND + 1):k + 1]) >= FP_DENSE

    start = next((k for k in range(n) if shared_from(k)), n)
    end = next((k + 1 for k in range(n - 1, start - 1, -1) if shared_to(k)), start)
    return start, end


def intro_from_sound(prints: dict[int, array]) -> dict[int, tuple[float, float]]:
    """For every file of a season, the opening titles it shares with others —
    where they begin and end is the median over every episode it shares them
    with, so a cue two episodes happen to repeat does not decide it."""
    found: dict[int, list[tuple[int, int]]] = {key: [] for key in prints}
    for (ka, fa), (kb, fb) in combinations(prints.items(), 2):
        ia, ib, n = _shared(fa, fb)
        start, end = _dense(fa, fb, ia, ib, n)
        if INTRO_SHORTEST_S <= (end - start) * FP_ITEM_S <= INTRO_LONGEST_S:
            found[ka].append((ia + start, ia + end))
            found[kb].append((ib + start, ib + end))
    titles = {}
    for key, spans in found.items():
        if not spans:
            continue
        start = statistics.median(a for a, _ in spans) * FP_ITEM_S
        end = statistics.median(b for _, b in spans) * FP_ITEM_S
        if end - start >= INTRO_SHORTEST_S:
            titles[key] = (start, end)
    return titles


def _agreeing(chapters: list[tuple[float, float]],
              sound: tuple[float, float]) -> tuple[float, float] | None:
    return next((c for c in chapters if abs(c[0] - sound[0]) <= CHAPTER_AGREES_S), None)


def intro_between(chapters: list[tuple[float, float]],
                  sound: tuple[float, float] | None) -> tuple[float, float] | None:
    """The chapter that agrees with the sound says where the titles begin: the
    sound can begin seconds early, on music laid over the last shot of the story
    before them. Where they end is the later of the two. A streaming service's
    intro marker stops at the title sequence and leaves out the cards after it
    that still carry its music — Silo's "Created by" and "Written by", Stranger
    Things' chapter title — and the sound hears those through to the first line
    of the story."""
    if sound is None:
        return None
    chapter = _agreeing(chapters, sound)
    if chapter is None:
        return sound
    return chapter[0], max(chapter[1], sound[1])


def borrowed(chapters, duration: float, sound: tuple[float, float] | None) -> bool:
    if chapters and not _table(chapters, duration):
        return True
    named = intro_chapters(chapters, duration)
    return bool(named) and sound is not None and _agreeing(named, sound) is None


async def _openings(unopened: list, reachable: list) -> tuple[list, dict[int, tuple[float, float]], set[int]]:
    """The files read this time, what was found in them, and which of them carry
    another cut's chapters. A season is heard a few episodes at a time — every
    pair of them is compared — and the loop comes back for the rest. An episode
    heard before without titles is heard again beside the new ones: a season's
    first episode has nothing yet to share them with."""
    hearable = [f for f in reachable if opening_window(f.duration_s) >= INTRO_SHORTEST_S]
    listen = [f for f in unopened if f in hearable][:SEASON_PEERS]
    opened = listen + [f for f in unopened if f not in hearable]
    peers = listen + [f for f in hearable if f not in unopened][:max(0, SEASON_PEERS - len(listen))]
    sounds: dict[int, tuple[float, float]] = {}
    if listen and len(peers) >= 2:
        prints = {f.id: await _opening_print(f.path, f.duration_s) for f in peers}
        sounds = await asyncio.to_thread(intro_from_sound, prints)
        opened += [f for f in peers if f not in unopened and f.intro_s is None]
    titles, foreign = {}, set()
    for media in opened:
        sound = sounds.get(media.id)
        found = intro_between(intro_chapters(media.chapters, media.duration_s), sound)
        if found is not None:
            titles[media.id] = found
        if borrowed(media.chapters, media.duration_s, sound):
            foreign.add(media.id)
    return opened, titles, foreign


async def _season(session, season_id: int) -> int:
    files = list((await session.execute(
        select(VideoFile).join(Episode, VideoFile.episode_id == Episode.id)
        .where(Episode.season_id == season_id, VideoFile.duration_s.is_not(None))
        .options(selectinload(VideoFile.chapters))
        .order_by(Episode.number))).scalars())
    unread = [f for f in files if f.credits_read_at is None]
    unopened = [f for f in files if f.intro_read_at is None]
    if not unread and not unopened:
        return 0
    reachable = [f for f in files if os.path.exists(f.path)]
    opened, titles, foreign = await _openings(unopened, reachable)
    unread += [f for f in opened if f.id in foreign and f not in unread]
    pictures: dict[int, float | None] = {}
    for media in unread:
        media.credits_s = None if media.id in foreign else from_chapters(media.chapters, media.duration_s)
        if media.credits_s is None and media in reachable:
            pictures[media.id] = await from_picture(media.path, media.duration_s)

    sounds: dict[int, float] = {}
    if any(at is None for at in pictures.values()):
        peers = [f for f in reachable if f.id in pictures]
        peers += [f for f in reachable if f.id not in pictures][:max(0, SEASON_PEERS - len(peers))]
        if len(peers) >= 2:
            prints = {f.id: await _fingerprint(f.path, f.duration_s) for f in peers}
            # a season's comparison is seconds of arithmetic, and the loop it
            # runs on also serves every request the library answers
            sounds = await asyncio.to_thread(from_sound, prints)

    now = datetime.datetime.now(datetime.UTC)
    for media in unread:
        if media.id in pictures:
            picture = pictures[media.id]
            media.credits_s = picture if picture is not None else sounds.get(media.id)
        media.credits_read_at = now
    for media in opened:
        media.intro_s, media.intro_end_s = titles.get(media.id, (None, None))
        media.intro_read_at = now
    await session.commit()
    return len({f.id for f in unread + opened})


async def credits_loop():
    await asyncio.sleep(180)
    while True:
        read = 0
        try:
            async with SessionLocal() as session:
                season_id = await session.scalar(
                    select(Episode.season_id).join(VideoFile, VideoFile.episode_id == Episode.id)
                    .join(Season, Season.id == Episode.season_id)
                    .where(or_(VideoFile.credits_read_at.is_(None), VideoFile.intro_read_at.is_(None)),
                           VideoFile.duration_s.is_not(None))
                    .order_by(Season.id.desc()).limit(1))
                if season_id is not None:
                    read = await _season(session, season_id)
                    log.info("credits: season %s, %d file(s) read", season_id, read)
        except Exception:
            log.exception("credits: a season could not be read")
        await asyncio.sleep(5 if read else IDLE_S)
