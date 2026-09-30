"""The photograph half, keeping itself up to date.

Every step here already exists and every one of them already knows how to skip
what it has done: the scan looks for files it has not seen, the metadata pass for
photographs with no date read yet, the derivatives for a generation that has
moved on, the face pass for pictures it has not looked at. What was missing was
anybody to call them in order.

So this calls them, in the order the facts depend on each other, and does
nothing at all when there is nothing new — which is almost always. A person who
drops a folder of last summer into the library should find those photographs
dated, cut, read for faces, placed on the map and, where the faces are people the
library already knows, named. Without asking for any of it.

The one step that is not merely "run the pass again" is the grouping. It is
built per year and rebuilding it is expensive, so it runs only when a year has
gained faces — and then the new groups are offered to the people already known,
which is the step that makes a photograph from this summer say who is in it.
"""

import asyncio
import datetime
import logging

from sqlalchemy import text

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, TIME_NONE
from opus.passes import Refused
from opus.photos.library import derive, metadata, place, places, scan
from opus.photos.people import ages, cluster, covers, detect, video
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# How often to look. The library changes when somebody puts something in it,
# which is a few times a year, so this is a heartbeat and not a poll of anything
# expensive: every step below asks one indexed question and stops.
EVERY = 300.0

# How many new faces a year must have gained before the grouping is redone. It is
# the one step that rewrites work already done, so a single photograph of a
# landscape should not rebuild anybody's year. Nothing is lost when it does run:
# a name lives on the faces, and a fresh group inherits whoever its faces already
# belong to.
ENOUGH_NEW = 8

# How long to wait after a pass refuses. A face pass with no GPU behind it says
# so and stops; asking again every five minutes turns one broken thing into a
# log full of the same sentence.
AFTER_TROUBLE = 3600.0

state: dict = {
    "watching": False,
    "doing": "",
    "last_look": None,
    "last_work": None,
    "did": {},
    # what refused, and why, so a broken step is visible rather than merely quiet
    "trouble": "",
}

# a photograph whose every file is gone cannot be read by any pass, and counted
# as waiting it would set one off on every heartbeat for good
_ON_DISK = "EXISTS (SELECT 1 FROM photo_files pf WHERE pf.photo_id = photos.id AND pf.state = 'present')"


async def _pending(session) -> dict:
    """What is waiting, asked as counts rather than as passes."""
    sharp = (await current_runtime()).float("faces_min_sharpness")
    row = (await session.execute(text(f"""
        SELECT
          (SELECT count(*) FROM photos
            WHERE taken_source = :undated AND {_ON_DISK}) AS undated,
          (SELECT count(*) FROM photos
            WHERE derived_gen <> :dgen AND derive_failed_gen <> :dgen
              AND {_ON_DISK}) AS underived,
          (SELECT count(*) FROM photos
            WHERE kind = 'image' AND derived_gen = :dgen
              AND faces_gen <> :fgen) AS unlooked,
          -- Recordings are counted apart because they are looked at by a
          -- different pass: a still is read from its derivative, a recording
          -- has frames cut out of it. Folded into one number, a library with
          -- six hundred unwatched videos would have sent the still pass off to
          -- do nothing, every five minutes, forever.
          (SELECT count(*) FROM photos
            WHERE kind = 'video' AND faces_gen <> :fgen AND {_ON_DISK}) AS unwatched,
          -- a face too blurred to be grouped is left out of every grouping,
          -- and counted as waiting it would start one on every heartbeat
          (SELECT count(*) FROM photo_faces
            WHERE generation = :fgen AND cluster_id IS NULL
              AND (sharpness IS NULL OR sharpness >= :sharp)) AS ungrouped,
          (SELECT count(*) FROM photos
            WHERE latitude IS NOT NULL AND place = '' AND place_source = '') AS unplaced
    """), {"dgen": derive.GENERATION, "fgen": FACE_GENERATION,
           "undated": TIME_NONE, "sharp": sharp})).one()
    return {"undated": row[0], "underived": row[1], "unlooked": row[2],
            "unwatched": row[3], "ungrouped": row[4], "unplaced": row[5]}


async def _step(name: str, work) -> dict:
    state["doing"] = name
    try:
        return await work()
    except Refused as why:
        state["trouble"] = f"{name}: {why}"
        log.warning("photos: %s refused: %s", name, why)
    except Exception as exc:
        state["trouble"] = f"{name}: {exc}"
        log.exception("photos: %s failed", name)
    return {}


async def _joined(name: str, job, work, *args) -> dict:
    """A pass that can also be started by hand: joined if it already runs."""
    state["doing"] = name
    done = await job.run(work, *args)
    if done.get("phase") in ("error", "refused"):
        state["trouble"] = f"{name}: {done.get('error')}"
    return done


async def once() -> dict:
    """One look, and whatever it leads to. Every pass skips what it has done."""
    did: dict[str, int] = {}
    state["trouble"] = ""

    # First, because everything below is a question about rows and a photograph
    # nobody has looked at yet has no row to be asked about. A walk of the tree
    # finds nothing almost every time and costs a stat per file; the four inode
    # columns are what make that cheap.
    did["found"] = (await _joined("scanning", scan.job, scan.walk_and_adopt)).get("added", 0)

    async with SessionLocal() as session:
        waiting = await _pending(session)

    if waiting["undated"]:
        did["dated"] = (await _joined("metadata", metadata.job, metadata.read,
                                      False)).get("dated", 0)
    if waiting["underived"]:
        did["derived"] = (await _joined("derivatives", derive.job, derive.make,
                                        False, False)).get("made", 0)
    if waiting["unlooked"]:
        did["faces"] = (await _step("faces", detect.run)).get("faces", 0)

    # After the stills and before the grouping, because a recording's faces are
    # faces like any other once they are written, and the grouping should see
    # them in the same pass rather than five minutes later.
    if waiting["unwatched"]:
        did["watched"] = (await _step("recordings", video.run)).get("faces", 0)

    # The grouping, but only where a year has gained enough to be worth redoing.
    # It is the one step that rewrites what is already there.
    if waiting["ungrouped"] >= ENOUGH_NEW:
        grouped = await _step("grouping", cluster.run)
        did["groups"] = grouped.get("clusters", 0)
        did["recognised"] = grouped.get("named", 0)
        did["aged"] = (await _step("ages", ages.run)).get("aged", 0)

    # after the grouping, because it is the grouping that says whose a face is
    did["covers"] = (await _step("covers", covers.run)).get("chosen", 0)

    if waiting["unplaced"]:
        did["placed"] = (await _step("places", places.run)).get("named", 0)

    # Last, and last on purpose. An offered picture waits in one folder until
    # everything that reads it has finished with it — deriving is the one pass
    # that opens the file by its path, and a rename underneath it would be a
    # missing file rather than a moved one. By here the picture is dated,
    # derived, looked at for faces and given its place name, and moving it is a
    # rename within one filesystem that nothing is watching.
    #
    # It asks only for a date, not for the rest to have gone well: a face pass
    # that failed must not strand somebody's photograph in a waiting room. It is
    # in the library either way, and the retry finds it at its new name.
    did["filed"] = (await place.settle())["placed"]

    did = {name: count for name, count in did.items() if count}
    state["doing"] = ""
    state["last_look"] = datetime.datetime.now(datetime.UTC).isoformat()
    if did:
        state["last_work"] = state["last_look"]
        state["did"] = did
        log.info("photos: %s", did)
    return did


async def watch_loop() -> None:
    """The heartbeat.

    Not behind a setting. There is no case where somebody wants photographs they
    have just added to sit there undated and unnamed until they remember to ask,
    and a switch that defaults to off is a switch that stays off until somebody
    wonders why nothing happened. If it ever needs to stop, that is a thing
    somebody does on purpose and can see, not a default."""
    state["watching"] = True
    try:
        while True:
            rest = EVERY
            try:
                await once()
                if state["trouble"]:
                    rest = AFTER_TROUBLE
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("photos: the watch stumbled")
                rest = AFTER_TROUBLE
            await asyncio.sleep(rest)
    finally:
        state["watching"] = False
