"""Non-destructive library adoption, the video half of it: walk the
movies/television libraries, identify each file (guessit → TMDB), register it in
place as a VideoFile linked to a Movie/Episode, and audit subtitles (embedded
streams + sidecar files next to the video). Files are NEVER moved, renamed or
retagged by the scan — it only reads them and writes DB rows.

Mirrors the music scan shape: one in-process pass started by
POST /api/video/library/scan, its progress polled over GET (no job table, no SSE). Idempotent and re-runnable —
VideoFile.path is unique, already-linked files are fast-gated, and a re-scan
refreshes a file's subtitle rows deterministically.
"""

import asyncio
import difflib
import json
import logging
import re
import unicodedata
from pathlib import Path

from guessit import guessit
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.passes import Pass
from opus.video.metadata import tmdb
from opus.models import (Chapter, Episode, MediaStream, VideoFile, Movie, Season, Series, Setting,
                         Subtitle)
from opus.settings_store import RuntimeConfig, current_runtime
from opus.video.subtitles.policy import effective_policy, evaluate
from opus.video.subtitles import extract, fetcher, opensubtitles, timing
from opus.video.subtitles.probe import own_sidecars, probe_file

log = logging.getLogger(__name__)

TITLE_MATCH_THRESHOLD = 0.6  # normalized title similarity to accept a TMDB hit

job = Pass("video library scan", total=0, processed=0, movies=0, series=0, episodes=0,
           unmatched=0, green=0, current="", results=[])


def _record(path: Path, outcome: str, **extra) -> None:
    job.state["results"].append({"file": path.name, "path": str(path), "outcome": outcome, **extra})


def _norm(s: str) -> str:
    # accents are folded, not dropped: dropping them made TÁR "t r", and the
    # older film called Tar won on its title
    folded = "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                     if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", folded).strip()


def find_video_files(directory: Path) -> list[Path]:
    from opus.video.subtitles.probe import find_video_files as _f
    return _f(directory)


def _title_variants(guess: dict) -> list[str]:
    """TMDB query strings to try, in priority order. Handles 'AKA' (original +
    international title glued together, e.g. 'Anatomie ... AKA Anatomy of a
    Fall') and guessit splitting 'X - Y' into title=X + alternative_title=Y
    when the real name is 'X: Y' (e.g. 'The Lord of the Rings: The Rings of
    Power')."""
    title = (guess.get("title") or "").strip()
    alt = (guess.get("alternative_title") or "").strip()
    variants: list[str] = []
    if title and alt:
        variants.append(f"{title} {alt}")  # recombine the split full name
    if title:
        variants.append(title)
    if alt:
        variants.append(alt)
    for part in re.split(r"\baka\b", title, flags=re.IGNORECASE):
        variants.append(part.strip())
    seen, out = set(), []
    for v in variants:
        key = _norm(v)
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _score(want: str, r: dict, year: int | None) -> float:
    ct, cot = _norm(r.get("title", "")), _norm(r.get("original_title", ""))
    s = max(difflib.SequenceMatcher(None, want, ct).ratio(),
            difflib.SequenceMatcher(None, want, cot).ratio())
    # a TMDB title that is the core of the query is a strong hit (TMDB lists the
    # show as "Lioness" for a "Special Ops Lioness" file)
    contained = len(ct) >= 4 and ct in want
    if contained:
        s = max(s, 0.9)
    if year and r.get("year") == year:
        s += 0.3
    # short queries fuzzy-match unrelated short titles (Bron ↔ Bronk); demand an
    # exact or containment hit for them rather than a 1-char-off ratio
    if len(want) <= 5 and ct != want and cot != want and not contained:
        s = min(s, 0.5)
    return s


TOP_RESULTS = 6  # TMDB ranks the real match high; scoring far-down results only
                 # invites fuzzy false positives ("Bron" → "Bronk")


def _exact(want: str, r: dict, year: int | None) -> bool:
    return bool(year) and r.get("year") == year and want in (
        _norm(r.get("title", "")), _norm(r.get("original_title", "")))


def _best(gathered: list[tuple[str, list[dict]]], year: int | None) -> list[dict]:
    """Every TMDB hit sharing the best score across every (query-variant, results)
    pair: two films of one name and one year are a tie no name can break."""
    best, best_score = [], 0.0
    for variant, results in gathered:
        want = _norm(variant)
        for rank, r in enumerate(results):
            # the exact name in the year asked for is no fuzzy match, however far
            # down TMDB ranks it this month (Help, 2021, fell to seventh)
            if rank >= TOP_RESULTS and not _exact(want, r, year):
                continue
            s = _score(want, r, year)
            if s > best_score:
                best, best_score = [r], s
            elif s == best_score and all(b.get("tmdb_id") != r.get("tmdb_id") for b in best):
                best.append(r)
    return best if best_score >= TITLE_MATCH_THRESHOLD else []


def _match(gathered: list[tuple[str, list[dict]]], year: int | None) -> dict | None:
    best = _best(gathered, year)
    return best[0] if best else None


async def _register_file(session, path: Path, *, movie_id: int | None = None,
                         episode_id: int | None = None) -> VideoFile:
    """Probe and register the file in place, (re)building its subtitle rows from
    embedded streams and stem-matched sidecars. Never touches the files."""
    info = await probe_file(path)
    existing = await session.execute(select(VideoFile).where(VideoFile.path == str(path)))
    media = existing.scalar_one_or_none()
    if media is None:
        media = VideoFile(path=str(path))
        session.add(media)
    else:
        await session.execute(delete(Subtitle).where(Subtitle.file_id == media.id))
    media.size = path.stat().st_size
    media.container = info["container"]
    media.video_codec = info["video_codec"]
    media.width, media.height = info["width"], info["height"]
    media.audio_langs = info["audio_langs"]
    media.duration_s = info.get("duration_s")
    media.movie_id, media.episode_id = movie_id, episode_id
    await session.flush()

    # what the file holds, recorded where the file is recorded — replaced whole,
    # because a re-probe describes the file as it is now
    await session.execute(delete(MediaStream).where(MediaStream.file_id == media.id))
    for stream in info.get("streams") or []:
        session.add(MediaStream(file_id=media.id, **stream))
    await session.execute(delete(Chapter).where(Chapter.file_id == media.id))
    for chapter in info.get("chapters") or []:
        session.add(Chapter(file_id=media.id, **chapter))

    for i, stream in enumerate(info["subtitle_streams"]):
        session.add(Subtitle(file_id=media.id, lang=stream["lang"], source="embedded",
                             format=stream["codec"], forced=stream["forced"],
                             stream_index=i))
    for sub in own_sidecars(path):
        auto = ".auto." in Path(sub["path"]).name.lower()
        session.add(Subtitle(file_id=media.id, lang=sub["lang"], source="external",
                             format=sub["format"], forced=sub["forced"],
                             auto_generated=auto, path=sub["path"]))
    await session.flush()
    return media


async def _count_green(session, config: RuntimeConfig, media: VideoFile,
                       override: str) -> None:
    subs = (await session.execute(
        select(Subtitle).where(Subtitle.file_id == media.id))).scalars().all()
    policy = effective_policy(config, override or "")
    if evaluate(policy, subs, accept_auto=config.bool("accept_auto_subs")).satisfied:
        job.state["green"] += 1


async def _get_or_create_movie(session, config: RuntimeConfig, tmdb_id: int) -> Movie:
    existing = await session.execute(select(Movie).where(Movie.tmdb_id == tmdb_id))
    movie = existing.scalar_one_or_none()
    if movie is not None:
        return movie
    details = await tmdb.movie_details(config, tmdb_id)
    movie = Movie(**details, monitored=False)  # adopted: we already have the file
    session.add(movie)
    await session.flush()
    return movie


def movie_guess(path: Path, root: Path) -> dict:
    """Read together with its folder: the folder is where a film's name is kept
    (`Title (Year)/`), while a disc's stream is called `00000.m2ts` and a film
    of that name exists."""
    try:
        return guessit(str(path.relative_to(root)))
    except ValueError:
        return guessit(path.name)


async def movie_candidates(config: RuntimeConfig, guess: dict) -> list[dict]:
    gathered = []
    for variant in _title_variants(guess):
        gathered.append((variant, await tmdb.search_movies(config, variant)))
    return _best(gathered, guess.get("year"))


async def _adopt_movie(session, config: RuntimeConfig, path: Path) -> None:
    job.state["current"] = path.name
    existing = await session.execute(select(VideoFile).where(VideoFile.path == str(path)))
    media = existing.scalar_one_or_none()
    if media is not None and media.movie_id is not None:
        _record(path, "already_adopted")
        return
    guess = movie_guess(path, Path(config.get("movies_dir")))
    title = guess.get("title")
    if not title:
        _record(path, "unparsed")
        job.state["unmatched"] += 1
        return
    try:
        found = await movie_candidates(config, guess)
    except tmdb.TmdbError as exc:
        _record(path, "tmdb_error", detail=str(exc))
        return
    match = found[0] if found else None
    if match is None:
        _record(path, "unmatched", guessed=str(title))
        job.state["unmatched"] += 1
        return
    movie = await _get_or_create_movie(session, config, match["tmdb_id"])
    media = await _register_file(session, path, movie_id=movie.id)
    await _count_green(session, config, media, movie.subtitle_override)
    job.state["movies"] += 1
    _record(path, "movie", title=movie.title, year=movie.year)


async def _create_series_tree(session, config: RuntimeConfig, tmdb_id: int) -> Series:
    details = await tmdb.series_details(config, tmdb_id)
    seasons = details.pop("seasons")
    series = Series(**details, monitored=False)
    session.add(series)
    await session.flush()
    for s in seasons:
        season = Season(series_id=series.id, number=s["number"], monitored=False,
                        overview=s["overview"], overview_hr=s["overview_hr"],
                        poster_url=s["poster_url"])
        session.add(season)
        await session.flush()
        for e in s["episodes"]:
            session.add(Episode(season_id=season.id, tmdb_id=e["tmdb_id"],
                                number=e["number"], title=e["title"],
                                air_date=e["air_date"], overview=e["overview"],
                                overview_hr=e.get("overview_hr", ""),
                                still_url=e.get("still_url"), runtime_min=e.get("runtime_min"),
                                monitored=False))
    await session.flush()
    job.state["series"] += 1
    return series


async def _resolve_series(session, config: RuntimeConfig, guess: dict) -> Series | None:
    gathered = []
    for variant in _title_variants(guess):
        try:
            gathered.append((variant, await tmdb.search_series(config, variant)))
        except tmdb.TmdbError as exc:
            log.warning("TMDB series search failed for %s: %s", variant, exc)
    match = _match(gathered, guess.get("year"))
    if match is None:
        return None
    existing = await session.execute(select(Series).where(Series.tmdb_id == match["tmdb_id"]))
    series = existing.scalar_one_or_none()
    if series is not None:
        return series
    return await _create_series_tree(session, config, match["tmdb_id"])


async def _adopt_episode(session, config: RuntimeConfig, path: Path,
                         series_cache: dict) -> None:
    job.state["current"] = path.name
    existing = await session.execute(select(VideoFile).where(VideoFile.path == str(path)))
    media = existing.scalar_one_or_none()
    if media is not None and media.episode_id is not None:
        _record(path, "already_adopted")
        return
    guess = guessit(path.name)
    title = guess.get("title")
    season_no, ep_no = guess.get("season"), guess.get("episode")
    if isinstance(season_no, list):
        season_no = season_no[0]
    if isinstance(ep_no, list):
        ep_no = ep_no[0]
    if not title or season_no is None or ep_no is None:
        _record(path, "unparsed")
        job.state["unmatched"] += 1
        return

    key = _norm(title)
    if key not in series_cache:
        # the facts rather than the row: a rollback expires every row the
        # session holds, and a later attribute read would be a lazy load
        found = await _resolve_series(session, config, guess)
        series_cache[key] = ((found.id, found.title, found.subtitle_override)
                             if found is not None else None)
    series = series_cache[key]
    if series is None:
        _record(path, "series_unmatched", guessed=str(title))
        job.state["unmatched"] += 1
        return
    series_id, series_title, override = series

    result = await session.execute(
        select(Episode).join(Season).where(
            Season.series_id == series_id, Season.number == season_no,
            Episode.number == ep_no))
    episode = result.scalar_one_or_none()
    if episode is None:
        _record(path, "episode_not_in_tmdb", series=series_title,
                s=season_no, e=ep_no)
        job.state["unmatched"] += 1
        return

    media = await _register_file(session, path, episode_id=episode.id)
    await _count_green(session, config, media, override)
    job.state["episodes"] += 1
    _record(path, "episode", series=series_title, s=season_no, e=ep_no)


async def scan() -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        ok, detail = await tmdb.health(config)
        if not ok:
            raise RuntimeError(f"TMDB not ready: {detail}")

        movies_dir = Path(config.get("movies_dir"))
        tv_dir = Path(config.get("tv_dir"))
        movie_files = (await asyncio.to_thread(find_video_files, movies_dir)
                       if movies_dir.exists() else [])
        tv_files = (await asyncio.to_thread(find_video_files, tv_dir)
                    if tv_dir.exists() else [])
        ignored = await _load_ignored(session)
        if ignored:
            movie_files = [f for f in movie_files if str(f) not in ignored]
            tv_files = [f for f in tv_files if str(f) not in ignored]
        state["total"] = len(movie_files) + len(tv_files)

        state["phase"] = "movies"
        for path in movie_files:
            try:
                await _adopt_movie(session, config, path)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                log.exception("adopt movie failed: %s", path)
                _record(path, "error", detail=str(exc))
            state["processed"] += 1

        state["phase"] = "tv"
        series_cache: dict = {}
        for path in tv_files:
            try:
                await _adopt_episode(session, config, path, series_cache)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                # a series made inside the transaction just undone is not there
                series_cache.clear()
                log.exception("adopt episode failed: %s", path)
                _record(path, "error", detail=str(exc))
            state["processed"] += 1


# ------------------------------------------------ manual review resolution ---

IGNORED_KEY = "library_ignored"  # a Setting row holding a JSON list of paths


def drop_result(path: str) -> None:
    state = job.state
    """Remove a review item from the live scan report after it's resolved."""
    before = len(state["results"])
    state["results"] = [r for r in state["results"] if r.get("path") != path]
    if len(state["results"]) < before:
        state["unmatched"] = max(0, state["unmatched"] - 1)


async def _load_ignored(session) -> set:
    row = await session.get(Setting, IGNORED_KEY)
    if row and row.value:
        try:
            return set(json.loads(row.value))
        except ValueError:
            return set()
    return set()


async def add_ignored(session, path: str) -> None:
    """Persist a path the scan should skip (a file that is legitimately not a
    TMDB movie/series, e.g. a featurette)."""
    paths = await _load_ignored(session)
    paths.add(path)
    value = json.dumps(sorted(paths))
    await session.execute(
        pg_insert(Setting).values(key=IGNORED_KEY, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value}))
    await session.commit()


async def resolve_path(session, config: RuntimeConfig, path_str: str,
                       media_type: str, tmdb_id: int) -> dict:
    """Manually adopt one file to a chosen TMDB movie/series (used to fix a
    review item the auto-matcher couldn't resolve — e.g. a foreign title)."""
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(path_str)
    if media_type == "movie":
        movie = await _get_or_create_movie(session, config, tmdb_id)
        media = await _register_file(session, path, movie_id=movie.id)
        await _count_green(session, config, media, movie.subtitle_override)
        await session.commit()
        return {"kind": "movie", "title": movie.title}

    existing = await session.execute(select(Series).where(Series.tmdb_id == tmdb_id))
    series = existing.scalar_one_or_none() or await _create_series_tree(session, config, tmdb_id)
    guess = guessit(path.name)
    season_no, ep_no = guess.get("season"), guess.get("episode")
    if isinstance(season_no, list):
        season_no = season_no[0]
    if isinstance(ep_no, list):
        ep_no = ep_no[0]
    if season_no is None or ep_no is None:
        raise ValueError("could not parse the season/episode from the filename")
    result = await session.execute(
        select(Episode).join(Season).where(
            Season.series_id == series.id, Season.number == season_no,
            Episode.number == ep_no))
    episode = result.scalar_one_or_none()
    if episode is None:
        raise ValueError(f"S{season_no:02d}E{ep_no:02d} is not in {series.title} on TMDB")
    await _register_file(session, path, episode_id=episode.id)
    await session.commit()
    return {"kind": "episode", "series": series.title, "s": season_no, "e": ep_no}


# --------------------------------------------------- subtitle extraction ----

def _wanted_tracks(video: Path, subtitles, langs: list[str]) -> list[dict]:
    """Which embedded tracks are worth reading out: the ones in a language this
    house asks for, that are text rather than pictures, and that are not already
    sitting beside the video, read out of the stream they actually are.

    That last clause is not pedantry. The number handed to ffmpeg used to be the
    row's place in the list Postgres returned, which is heap order and stops
    matching stream order the moment a row is updated — so a track could be, and
    was, demuxed from a neighbour. A .vtt whose name says a different stream than
    the row does is that mistake on disk; it goes, and the track is read again."""
    wanted = []
    for sub in subtitles:
        if sub.source != "embedded" or sub.stream_index is None or sub.hollow:
            continue
        if (sub.format or "").lower() not in extract.TEXT_CODECS:
            continue
        if langs and sub.lang not in langs:
            continue
        expected = extract.vtt_for(video, sub.lang, sub.stream_index)
        if sub.vtt_path == str(expected) and expected.exists():
            continue
        if sub.vtt_path and sub.vtt_path != str(expected):
            Path(sub.vtt_path).unlink(missing_ok=True)
            sub.vtt_path = None
        wanted.append({"position": sub.stream_index, "lang": sub.lang, "row": sub})
    return wanted


async def _extract_all() -> None:
    """Read the wanted subtitles out of every file that still has some inside it.

    One pass per FILE, not per track: the cost is reading the container, and a
    remux read five times for five languages is five times three minutes."""
    async with SessionLocal() as session:
        config = await current_runtime()
        langs = config.langs()
        ids = list((await session.execute(select(VideoFile.id))).scalars())
    written = hollow = failed = 0
    for file_id in ids:
        async with SessionLocal() as session:
            media = (await session.execute(
                select(VideoFile).where(VideoFile.id == file_id)
                .options(selectinload(VideoFile.subtitles))
            )).scalar_one_or_none()
            if media is None or not Path(media.path).exists():
                continue
            wanted = _wanted_tracks(Path(media.path), media.subtitles, langs)
            if not wanted:
                continue
            try:
                got = await extract.extract(
                    Path(media.path),
                    [{"position": w["position"], "lang": w["lang"]} for w in wanted])
            except OSError as exc:
                log.warning("extract: %s: %s", media.path, exc)
                failed += 1
                got = {}
            for w in wanted:
                if w["position"] not in got:
                    continue
                path = got[w["position"]]
                if path:
                    w["row"].vtt_path = path
                    written += 1
                else:
                    w["row"].hollow = True
                    hollow += 1
            await session.commit()
    log.info("subtitle extraction done: %d tracks written, %d hollow, %d files failed",
             written, hollow, failed)


async def resume_extract() -> None:
    """Pick the backfill up again after a restart.

    It runs as a task inside this process, so every deploy killed it and it
    stayed dead until a person noticed — four times in one day. Nothing about
    it needs a person: it skips what has already been written, so resuming is
    the same act as starting."""
    await asyncio.sleep(20)
    try:
        async with SessionLocal() as session:
            langs = (await current_runtime()).langs()
            pending = (await session.execute(
                select(func.count()).select_from(Subtitle).where(
                    Subtitle.source == "embedded",
                    Subtitle.hollow.is_(False),
                    Subtitle.format.in_(tuple(extract.TEXT_CODECS)),
                    *([Subtitle.lang.in_(langs)] if langs else []),
                    or_(Subtitle.vtt_path.is_(None),
                        # written out of a stream that was not this one
                        Subtitle.vtt_path.notlike(
                            func.concat("%.", Subtitle.lang, ".",
                                        Subtitle.stream_index, ".opus.vtt")))))).scalar()
        if pending:
            log.info("subtitle extraction resumes: %s tracks still unread", pending)
            await _extract_all()
    except Exception:
        log.exception("subtitle extraction failed")


# ----------------------------------------------- subtitle re-acquisition ----

# discarded: wrong subtitles removed. refetched: replacements that fitted.
# unfixed: asked for and the provider had nothing that fits. owed: never asked,
# because the day's allowance was gone.
resub = Pass("subtitle refetch", timed=False, total=0, done=0, discarded=0, refetched=0,
             unfixed=0, owed=0, remaining=None, detail="")


async def _override_for(session, media: VideoFile) -> str:
    """The subtitle policy this file answers to, which lives on the film or on
    the series rather than on the file."""
    if media.movie_id:
        movie = await session.get(Movie, media.movie_id)
        return movie.subtitle_override if movie else ""
    if media.episode_id:
        override = await session.scalar(
            select(Series.subtitle_override)
            .join(Season, Season.series_id == Series.id)
            .join(Episode, Episode.season_id == Season.id)
            .where(Episode.id == media.episode_id))
        return override or ""
    return ""


def _misfitting(media: VideoFile) -> list:
    """This file's sidecars that were written for another cut. Measured against
    the running time, which costs a read of the subtitle and nothing of the
    video."""
    out = []
    for sub in media.subtitles:
        if not sub.path:
            continue
        fit = timing.check(sub.path, media.duration_s)
        if fit.mismatch:
            log.info("discarding %s: it is not a subtitle of this file — %s",
                     sub.path, timing.describe(fit, media.duration_s))
            out.append(sub)
    return out


async def resubtitle() -> None:
    """Throw out every sidecar that belongs to another release and ask for what
    the policy is short of — one file at a time, all the way through.

    A subtitle for another cut is thrown away the moment it is found, always,
    whatever else the run can or cannot do. Removing one costs nothing — no
    request, no allowance — and keeping one costs everything the audit was for:
    it is offered in the menu, it plays, and it is wrong from the first minute.
    Only the replacing is paid for, so only the replacing can run out.

    When it does, the run keeps auditing to the end and stops asking. Every
    wrong subtitle in the library is gone by the time it finishes; what is still
    short is short in the catalogue, which is where the next run reads its work
    from."""
    resub_state = resub.state
    async with SessionLocal() as session:
        config = await current_runtime()
        file_ids = list((await session.execute(
            select(VideoFile.id).order_by(VideoFile.id))).scalars())
    accept_auto = config.bool("accept_auto_subs")
    resub_state["total"] = len(file_ids)
    broke = False  # the allowance ran out; keep auditing, stop asking
    # the allowance is not asked for up front: the only endpoint that would
    # answer reports a number that is not true. It becomes known with the
    # first download of the run and guards every file after it.
    opensubtitles.spent["remaining"] = None

    for file_id in file_ids:
        async with SessionLocal() as session:
            media = (await session.execute(
                select(VideoFile).where(VideoFile.id == file_id)
                .options(selectinload(VideoFile.subtitles)))).scalar_one_or_none()
            if media is None or not Path(media.path).exists():
                resub_state["done"] += 1
                continue
            bad = await asyncio.to_thread(_misfitting, media)
            policy = effective_policy(config, await _override_for(session, media))
            dropping = {s.id for s in bad}
            keeping = [s for s in media.subtitles if s.id not in dropping]
            short = evaluate(policy, keeping, accept_auto=accept_auto).missing
            wanted = sorted(({s.lang for s in bad} | set(short)) & set(policy.langs))
            if not bad and not wanted:
                resub_state["done"] += 1
                continue

            for sub in bad:
                Path(sub.path).unlink(missing_ok=True)
                await session.execute(delete(Subtitle).where(Subtitle.id == sub.id))
                resub_state["discarded"] += 1
            await session.flush()

            left = opensubtitles.spent["remaining"]
            if wanted and left is not None and left < len(wanted):
                broke = True
            if wanted and not broke:
                try:
                    saved = await fetcher.acquire_missing(session, config, media, wanted)
                    resub_state["refetched"] += len(saved)
                    resub_state["unfixed"] += len(wanted) - len(saved)
                except opensubtitles.QuotaExhausted:
                    broke = True
                resub_state["remaining"] = opensubtitles.spent["remaining"]
            if wanted and broke:
                resub_state["owed"] += len(wanted)
            await session.commit()
        resub_state["done"] += 1
    if broke:
        resub_state["detail"] = (
            f"today's allowance is spent; {resub_state['owed']} languages were "
            f"not asked for. Every wrong subtitle is gone — run this again "
            f"after the allowance renews at midnight UTC")
    log.info("subtitle re-acquisition done: %s discarded, %s replaced, "
             "%s not found, %s unasked", resub_state["discarded"],
             resub_state["refetched"], resub_state["unfixed"], resub_state["owed"])
