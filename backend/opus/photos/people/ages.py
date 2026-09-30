"""How old every face looks, so a person's birth year can be worked out rather
than typed in.

A face recogniser is built to be age-blind — that is its purpose — so the age
has to come from somewhere else. What it buys is the one thing similarity cannot
give: two sisters photographed at the same age three years apart are identical
to a recogniser and three years apart to this.

Nothing here decides anything. It writes a number per face; the birth year falls
out of it further up, as the date on the photograph minus the age on the face,
and it should be the same number from every picture of somebody ever taken.
"""

import logging

import httpx
from sqlalchemy import bindparam, text

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, Face
from opus.passes import Refused
from opus.photos.library import derive
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

BATCH = 96

# How many faces of a group are read. Not one, and not all of them.
#
# The error on a single face is 4.25 years, which decides nothing: the two
# sisters this exists to separate are three years apart. Averaging cuts the
# random half of that by the root of the count — twenty-five faces bring it to
# under a year — while the systematic half, the model reading children as older
# than they are, survives any amount of averaging and cancels in the difference
# anyway because it lands on both sisters alike.
#
# So one representative is useless and sixty is waste. Beyond this the pass buys
# nothing but time, and there are 67,492 faces in 16,656 groups.
PER_CLUSTER = 25

# What a reading means in years, and where it stops meaning anything.
#
# The model does not answer in years — it answers on a scale that happens to be
# labelled in them, and the two are not the same. Measured against five people in
# this library whose birth dates are known exactly, over 7,144 faces spanning
# ages 0 to 53: a reading of 1.7 is a child of 0.8, a reading of 16.6 is a child
# of 13.2, a reading of 25.5 is a young adult of 21.1. Children are read old, by
# two years at two and by five at seventeen, and the error is orderly enough to
# undo.
#
# Above that it collapses. Readings from 28.5 to 52.4 — twenty-four years of
# scale — cover true ages from 38.7 to 46.4, eight years of life. The model can
# tell a child's age and cannot tell an adult's, and no correction recovers what
# was never measured: it is not a bias to subtract but information that is
# absent. So a grown reading yields no age at all, which is the honest answer
# and the one that keeps a birth year from being invented.
#
# The cut is where the reading stops being unambiguous rather than where it stops
# being monotone. Adults cluster just under it — a man of 43 reads 25 — and a bin
# of readings from 24 to 27 holds a median true age of 21 with a mean of 24,
# which is two populations in one bin. Letting them through was worse than
# leaving them out: a grown face read as a young adult yields a birth year of the
# photograph's year minus twenty, so it drifts with the calendar, and the rule
# meant to separate two sisters then refuses to join any adult's own decades to
# each other. Below 21 there is one population and the reading means what it
# says.
#
# Deliberately NOT fitted above the cut. The apparent jump there — three years of
# reading crossing seventeen years of true age — is this family's age
# distribution showing through, children and their parents with nobody between,
# and fitting it would bake one household into everybody's code.
READING = [(1.7, 0.8), (4.9, 2.5), (7.1, 6.6), (10.4, 9.4), (13.5, 11.0),
           (16.6, 13.2), (19.6, 14.4)]
GROWN = 21.0


def in_years(column: str) -> str:
    """SQL turning a raw reading into an age in years, or NULL for a grown face.

    Kept as an expression over the stored reading rather than applied when the
    reading is written: the reading is the measurement and this is what we
    currently believe it means, and only one of those two should need a rerun of
    the GPU when it changes."""
    steps = []
    for (x0, y0), (x1, y1) in zip(READING, READING[1:]):
        slope = (y1 - y0) / (x1 - x0)
        steps.append(f"WHEN {column} < {x1} THEN {y0} + ({column} - {x0}) * {slope:.4f}")
    return (f"CASE WHEN {column} IS NULL OR {column} >= {GROWN} THEN NULL "
            f"WHEN {column} <= {READING[0][0]} THEN {READING[0][1]} "
            + " ".join(steps) + f" ELSE {READING[-1][1]} END")


async def run() -> dict:
    """Read the faces no age has been read for yet."""
    aged = 0
    async with SessionLocal() as session:
        config = await current_runtime()
        url = (config.get("faces_url") or "http://faces:8099").rstrip("/")
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                health = (await client.get(f"{url}/health")).json()
            except (httpx.HTTPError, ValueError) as exc:
                raise Refused(f"{url} did not answer: {exc}") from exc
            if not health.get("age"):
                raise Refused("the faces service has no age model. It is a FairFace "
                              "ViT converted to ONNX and staged on the models volume "
                              "as age.onnx — InsightFace's own cannot see children.")

            # the clearest faces of each group, since a blurred one at the
            # edge of a crowd tells you less about anybody's age
            rows = (await session.execute(text("""
                SELECT id, x, y, w, h, checksum FROM (
                    SELECT f.id, f.x, f.y, f.w, f.h, encode(p.checksum,'hex') AS checksum,
                           row_number() OVER (PARTITION BY f.cluster_id
                                              ORDER BY f.score DESC, f.id) AS rn
                    FROM photo_faces f JOIN photos p ON p.id = f.photo_id
                    WHERE f.generation = :gen AND f.cluster_id IS NOT NULL
                      AND f.apparent_age IS NULL
                ) t WHERE rn <= :per ORDER BY id
            """), {"gen": FACE_GENERATION, "per": PER_CLUSTER})).all()

            for start in range(0, len(rows), BATCH):
                chunk = rows[start:start + BATCH]
                try:
                    resp = await client.post(f"{url}/age", timeout=300, json={
                        "generation": derive.GENERATION,
                        "faces": [{"id": r[0], "x": float(r[1]), "y": float(r[2]),
                                   "w": float(r[3]), "h": float(r[4]), "checksum": r[5]}
                                  for r in chunk]})
                    resp.raise_for_status()
                    answers = resp.json()["results"]
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    log.warning("ages: batch failed: %s", exc)
                    continue
                # one statement for the batch, not one per face
                read = [{"b_id": a["id"], "b_age": a["apparent_age"]}
                        for a in answers if a["apparent_age"] is not None]
                if read:
                    await session.execute(
                        Face.__table__.update()
                        .where(Face.id == bindparam("b_id"))
                        .values(apparent_age=bindparam("b_age")), read)
                    aged += len(read)
                await session.commit()
    return {"aged": aged}
