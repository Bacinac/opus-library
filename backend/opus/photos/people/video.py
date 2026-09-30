"""Who is in a recording.

A recording is a photograph that lasts a while. The shelf already treats it as
one — it has a date, a place and a poster frame — but no person's page has ever
shown one, because the face pass reads a derivative and a recording's derivative
is a single frame taken one second in. One second into a twenty-minute
christening is a wall.

So this cuts frames across the whole of it. What it then does with them was
decided by a measurement rather than by taste.

**The first attempt grouped the faces among themselves** and kept one survivor
of each group, so that a recording would behave like a photograph in every pass
after it. On a twenty-minute christening that turned 2,182 faces into 563, not
into the dozen people in the room. The reason is the material: an interlaced PAL
tape gives soft crops, and the same face across frames sits at 0.63
nearest-neighbour on the median with a quarter of them below 0.54 — so the
density rule never forms a core and every face stays its own. Grouping video
faces by likeness is fighting the tape.

**So it asks the other question.** The library already knows what fifteen
hundred named groups look like, year by year. A frame is not asked "who else
here resembles you"; it is asked "are you anybody we know". Six hundred frames
are six hundred chances for one good look at somebody, and the five hundred poor
crops of them cost nothing because they simply match nobody.

On the same christening that answers twelve people, each against their own 2005
group, at 0.63 to 0.79 — and it refuses the child who was not born for another
three years, whose best likeness across all 2,182 faces reached 0.591.

What is written is one face per person recognised, at the moment they were seen
best. Nothing anonymous is kept: a recording is here to say who is in it, and a
face that resembles nobody the library knows is a smear on a tape.

The cuts are the recognition pass's own — the same similarity, the same "clearly
nearer than the next candidate" — because this is the same judgement it makes,
asked of a frame instead of a group.
"""

import asyncio
import base64
import logging
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import delete, select, text

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, FILE_PRESENT, Face, Photo, PhotoFile
from opus.passes import Refused
from opus.photos.people.recognise import CLEARLY, KNOWN
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# How often to cut a frame. Two seconds is short enough that somebody who walks
# through the shot is caught and long enough that the whole archive — eight and
# a half hours of it — is fifteen thousand frames rather than a million.
EVERY = 2.0

# The most frames one recording may contribute, so a single long tape cannot own
# the pass. At two seconds this is twenty minutes of even sampling; beyond it the
# spacing widens rather than the tail being dropped, because the end of a
# christening is not less interesting than the start.
MOST = 600

# How wide to decode. The still pass works from 2048 px previews for the same
# reason: a face worth naming is a hundred pixels across at that size, and an old
# PAL tape has no more detail than this to give.
WIDTH = 1280

# How many frames travel in one request. These are sent rather than read, so
# unlike the still pass this really is a payload — twenty-four 1280 px JPEGs is
# a couple of megabytes.
BATCH = 24

# How many frames must find the same person before the recording is said to hold
# them. One frame in six hundred is likelier to be a mismatch than a guest, and
# somebody actually in the room is seen again and again: on the measured
# christening the twelve people recognised were found in 20 to 186 frames each,
# and the child who was not there in exactly one.
LEAST_FRAMES = 3

def _length(path: Path) -> float:
    told = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, timeout=60)
    try:
        return max(0.0, float(told.stdout.strip()))
    except ValueError:
        return 0.0


def _step(length: float) -> float:
    """How far apart to cut, widening rather than truncating once a recording is
    long enough to hit the cap: the end of a christening is not less interesting
    than the start."""
    if length <= 0:
        return EVERY
    return max(EVERY, length / MOST)


def _cut(path: Path, into: Path, step: float) -> list[tuple[float, Path]]:
    """Every frame of one recording, in one decode.

    One ffmpeg for the whole file and not one per frame. Seeking to six hundred
    separate moments in a 228 MB DivX costs six hundred process starts and six
    hundred index walks — measured at several minutes for one tape, against
    twenty-seven seconds to decode the whole of it once. Over six hundred and
    thirty-eight recordings that is the difference between an afternoon and a
    coffee.
    """
    done = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
         "-vf", f"fps=1/{step:.4f},scale={WIDTH}:-2:flags=bicubic",
         # JPEG's own range, said out loud. Some phones record limited-range
         # YUV, and the encoder refuses it as non-standard rather than
         # converting — two recordings out of six hundred and thirty-eight
         # produced no frames at all and reported it as a decode failure
         "-pix_fmt", "yuvj420p",
         "-q:v", "3", "-f", "image2", str(into / "%05d.jpg")],
        capture_output=True, timeout=1800)
    frames = sorted(into.glob("*.jpg"))
    if not frames:
        # A recording shorter than one interval gets no frame from an fps
        # filter — a Live Photo is a second and a half, and there are hundreds
        # of them. One frame from the middle rather than none, because a second
        # and a half of somebody is still somebody.
        middle = into / "00001.jpg"
        again = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
             "-vf", f"scale={WIDTH}:-2:flags=bicubic", "-pix_fmt", "yuvj420p",
             "-frames:v", "1", "-q:v", "3", "-y", str(middle)],
            capture_output=True, timeout=300)
        if middle.exists() and middle.stat().st_size:
            return [(0.0, middle)]
        raise RuntimeError(
            f"nothing could be decoded: {(again.stderr or done.stderr).decode()[:200]}")
    # the filter emits one frame per interval from the start, so the nth file is
    # the nth interval — the timestamp is arithmetic and not something to read
    # back out of the file
    return [(round(i * step, 2), f) for i, f in enumerate(frames)]


async def _known_people(session) -> tuple[np.ndarray, list[tuple[int, str]]]:
    """Every named group's centroid, and whose it is.

    Read once for the whole pass rather than per recording: it is fifteen
    hundred vectors, it does not change while the pass runs, and asking the
    database six hundred times for the same answer is six hundred times.
    """
    import json as _json
    rows = (await session.execute(text("""
        SELECT c.person_id, pp.name, c.centroid::text
        FROM photo_face_clusters c
        JOIN photo_people pp ON pp.id = c.person_id
        WHERE c.centroid IS NOT NULL
    """))).all()
    if not rows:
        return np.zeros((0, 512), np.float32), []
    vecs = np.array([_json.loads(r[2]) for r in rows], dtype=np.float32)
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    return vecs, [(r[0], r[1]) for r in rows]


def _who_is_in_it(found: list[dict], known: np.ndarray,
                  owners: list[tuple[int, str]], cut: float = KNOWN,
                  clearly: float = CLEARLY, least: int = LEAST_FRAMES) -> list[dict]:
    """The people the library already knows, seen in this recording.

    Each frame's face is asked which named group it is nearest, and answers only
    if it is past the cut AND clearly nearer than the best group belonging to
    anybody else — a face that suits two people equally is not evidence about
    either. A person is in the recording if enough separate frames say so, and
    what is kept is the single frame that saw them best.
    """
    if not found or not len(known):
        return []
    vecs = np.array([f["embedding"] for f in found], dtype=np.float32)
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    sims = vecs @ known.T

    people = np.array([o[0] for o in owners])
    votes: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for i in range(len(found)):
        row = sims[i]
        j = int(row.argmax())
        best = float(row[j])
        if best < cut:
            continue
        # the nearest group belonging to somebody else, which is what makes this
        # face evidence about one person rather than about two
        other = row[people != people[j]]
        if other.size and float(other.max()) > best - clearly:
            continue
        votes[int(people[j])].append((best, i))

    kept = []
    for person_id, seen in votes.items():
        if len(seen) < least:
            continue
        best, i = max(seen)
        kept.append({**found[i], "person_id": person_id, "seen_in": len(seen),
                     "likeness": best})
    return kept


async def run() -> dict:
    counts = {"frames": 0, "faces": 0, "without": 0, "failed": 0}
    async with SessionLocal() as session:
        config = await current_runtime()
        url = (config.get("faces_url") or "http://faces:8099").rstrip("/")
        root = Path(config.get("photos_dir") or "/photos")

        async with httpx.AsyncClient(timeout=60) as client:
            try:
                health = (await client.get(f"{url}/health")).json()
            except (httpx.HTTPError, ValueError) as exc:
                raise Refused(f"the faces service at {url} did not answer: {exc}") from exc
            if not health.get("ok"):
                raise Refused(f"the faces service is up but has no models: {health}")

            known, owners = await _known_people(session)
            if not len(known):
                raise Refused("nobody is named yet, so there is nobody to recognise")

            videos = [(photo_id, path) for photo_id, path in (await session.execute(
                select(Photo.id, PhotoFile.path)
                .join(PhotoFile, PhotoFile.photo_id == Photo.id)
                .where(Photo.kind == "video", Photo.faces_gen != FACE_GENERATION,
                       PhotoFile.state == FILE_PRESENT)
                .distinct(Photo.id)
                .order_by(Photo.id, PhotoFile.id))).all()]

            for photo_id, where in videos:
                # the catalogue speaks the read-only name, which is the one
                # this container sees; anything outside the tree is a row
                # that has no business being read from
                path = Path(where)
                if not path.is_relative_to(root):
                    counts["failed"] += 1
                    log.warning("recordings: %s is outside %s", path, root)
                    continue
                try:
                    found = await _look(client, url, path, counts)
                except (httpx.HTTPError, OSError, RuntimeError, subprocess.SubprocessError,
                        ValueError, KeyError) as exc:
                    counts["failed"] += 1
                    log.warning("recordings: %s: %s", path.name, exc)
                    continue

                kept = _who_is_in_it(found, known, owners)
                await session.execute(delete(Face).where(
                    Face.photo_id == photo_id, Face.generation != FACE_GENERATION))
                for face in kept:
                    session.add(Face(
                        photo_id=photo_id, person_id=face["person_id"],
                        x=face["x"], y=face["y"], w=face["w"], h=face["h"],
                        score=face["score"], embedding=face["embedding"],
                        apparent_age=face.get("apparent_age"),
                        at_seconds=face["at"], generation=FACE_GENERATION))
                counts["faces"] += len(kept)
                if not kept:
                    counts["without"] += 1
                await session.execute(
                    Photo.__table__.update().where(Photo.id == photo_id)
                    .values(faces_gen=FACE_GENERATION))
                await session.commit()
    return counts


async def _look(client: httpx.AsyncClient, url: str, path: Path,
                counts: dict) -> list[dict]:
    """Every face in one recording, each carrying the moment it was seen.

    The frames are cut to a directory that lasts as long as the question and no
    longer. Sixty megabytes of them for the longest tape here — held on disk
    rather than in memory because the whole point of decoding once is not to be
    holding six hundred pictures while the service works through the first two
    dozen.
    """
    step = _step(await asyncio.to_thread(_length, path))
    found: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="frames-") as room:
        cut = await asyncio.to_thread(_cut, path, Path(room), step)
        for start in range(0, len(cut), BATCH):
            frames = [{"at": at, "jpeg": base64.b64encode(f.read_bytes()).decode()}
                      for at, f in cut[start:start + BATCH]]
            if not frames:
                continue
            counts["frames"] += len(frames)
            resp = await client.post(f"{url}/detect-frames", json={"frames": frames},
                                     timeout=300)
            resp.raise_for_status()
            for answer in resp.json()["results"]:
                if answer.get("error"):
                    continue
                for face in answer["faces"]:
                    found.append({**face, "at": answer["at"]})
    return found
