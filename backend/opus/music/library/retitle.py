"""Putting the catalogue's titles into the one form, and the files with it.

The words in a tag are not ours to change. Forty-six thousand of these files
were tagged from MusicBrainz and carry its recording id; what they call a song
is what the song is called, and `Lješka od ljubavi` is not a badly typed
`Ješka od jubavi` for the catalogue to correct. The catalogue is assembled for
finding and acquiring records, and on the ex-Yu shelf it is the weaker source
about names.

What IS ours is the form. MusicBrainz has no single house style — one editor
writes a Croatian title as a sentence, another capitalises every word — so the
shelf is levelled here: `display_title` decides the capitals, over words that
come from the file itself. A file with no title at all is the one case the
catalogue answers, because there is nothing else to answer with.

So nothing reads across from the catalogue into a file. It is safe to run twice:
the form is a function of the title, so a title already in it comes back
unchanged and the file is never opened.
"""

import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.passes import Pass
from opus.models import Artist, Release, Track
from opus.music.tagging import tagger
from opus.music.titlecase import display_title

log = logging.getLogger("opus.retitle")

job = Pass("retitle", timed=False, total=0, processed=0, releases=0, tracks=0, tagged=0,
           untitled=0, current=None, failed=[])


def _retag(path_text: str, write, value: str) -> bool:
    path = Path(path_text)
    if not path.exists():
        return False
    try:
        write(path, value)
        return True
    except Exception as exc:
        log.warning("retitle: could not tag %s: %s", path, exc)
        job.state["failed"].append(f"{path.name}: {exc}")
        return False


def _tags_of(media) -> dict:
    """What the row says the file says. Reading forty-eight thousand files to
    find the few hundred that differ is the reading this column exists to
    avoid — but a row probed before the column existed carries nothing yet, so
    the file answers once and the row keeps it."""
    if media.tags is None:
        media.tags = tagger.probe_file(Path(media.path)).get("tags") or {}
    return media.tags


async def retitle(write_tags: bool, artist_id: int | None) -> None:
    state = job.state
    try:
        async with SessionLocal() as session:
            query = (select(Release.id).join(Artist, Artist.id == Release.artist_id)
                     .where(Artist.monitored).order_by(Release.id))
            if artist_id is not None:
                query = query.where(Artist.id == artist_id)
            ids = list((await session.execute(query)).scalars())
        state["total"] = len(ids)

        for release_id in ids:
            async with SessionLocal() as session:
                release = (await session.execute(
                    select(Release).where(Release.id == release_id).options(
                        selectinload(Release.tracks).selectinload(Track.files)))
                ).scalar_one_or_none()
                if release is None:
                    state["processed"] += 1
                    continue
                state["current"] = release.title
                await asyncio.to_thread(_shape_release, release, write_tags)
                await session.commit()
            state["processed"] += 1
            # the walk holds no lock and nothing waits on it; yielding keeps the
            # queue and the shelf answering while it runs
            await asyncio.sleep(0)
    finally:
        log.info("retitle: %d records and %d songs renamed, %d tags written",
                 state["releases"], state["tracks"], state["tagged"])


def _shape_release(release: Release, write_tags: bool) -> None:
    state = job.state
    album = display_title(release.title)
    if album != release.title:
        release.title = album
        state["releases"] += 1

    for track in release.tracks:
        song = display_title(track.title)
        if song != track.title:
            track.title = song
            state["tracks"] += 1

        for media in track.files:
            if not media.path:
                continue
            held = _tags_of(media)
            for field, catalog in (("TITLE", song), ("ALBUM", album)):
                said = held.get(field)
                if said is None:
                    # nothing in the file to level; the catalogue is all there is
                    state["untitled"] += 1
                    wanted = catalog
                else:
                    wanted = display_title(said)
                if wanted == said:
                    continue
                write = (tagger.write_title_tag if field == "TITLE"
                         else tagger.write_album_tag)
                if not write_tags:
                    state["tagged"] += 1
                    continue
                if _retag(media.path, write, wanted):
                    media.tags = {**held, field: wanted}
                    held = media.tags
                    # the row keeps the tag twice: the whole of it in `tags`, and
                    # the four the matcher reads in columns of their own. Both are
                    # written, or the folder goes on matching the record it has
                    # just been corrected away from
                    if field == "TITLE":
                        media.tag_title = wanted
                    else:
                        media.tag_album = wanted
                    state["tagged"] += 1
