"""Measuring how sharp each face is.

Separate from finding the faces because it answers a different question and must
be able to answer it again: what counts as too blurred is a judgement, and a
judgement changes. The measurement is stored raw and the cut is applied when the
faces are used, so moving the cut costs a query rather than two hours.

It reads the same previews the detector read, and cuts the same crop the age
model is given — the face as a person would see it, hair and chin included.
"""

import logging

import httpx
from sqlalchemy import bindparam, func, select, text

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, Face
from opus.passes import Pass, Refused
from opus.photos.library import derive
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# Faces per request. They are cut from previews, and faces from one photograph
# travel together, so a batch is mostly a handful of files read once.
BATCH = 250

job = Pass("focus", total=0, processed=0, measured=0)


async def measure(remeasure: bool) -> None:
    state = job.state
    async with SessionLocal() as session:
        config = await current_runtime()
        url = (config.get("faces_url") or "http://faces:8099").rstrip("/")
        async with httpx.AsyncClient(timeout=300) as client:
            try:
                health = (await client.get(f"{url}/health")).json()
            except (httpx.HTTPError, ValueError) as exc:
                raise Refused(f"the faces service at {url} did not answer: {exc}") from exc
            if not health.get("ok"):
                raise Refused(f"the faces service is up but has no models: {health}")

            where = "" if remeasure else "AND f.sharpness IS NULL"
            rows = (await session.execute(text(f"""
                SELECT f.id, f.x, f.y, f.w, f.h, p.checksum
                FROM photo_faces f JOIN photos p ON p.id = f.photo_id
                WHERE f.generation = :gen {where}
                ORDER BY f.photo_id, f.id
            """), {"gen": FACE_GENERATION})).all()
            state["total"] = len(rows)
            state["phase"] = "reading"

            for start in range(0, len(rows), BATCH):
                chunk = rows[start:start + BATCH]
                try:
                    resp = await client.post(f"{url}/focus", json={
                        "faces": [{"id": r.id, "x": r.x, "y": r.y, "w": r.w,
                                   "h": r.h, "checksum": r.checksum.hex()}
                                  for r in chunk],
                        "generation": derive.GENERATION})
                    resp.raise_for_status()
                    answers = resp.json()["results"]
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    state["processed"] += len(chunk)
                    log.warning("focus: batch failed: %s", exc)
                    continue
                # one statement for the batch, not one per face: a round
                # trip each was most of the wall clock, and the work itself
                # is a Laplacian over a thumbnail
                got = [{"b_id": a["id"], "b_sharp": a["focus"]}
                       for a in answers if a.get("focus") is not None]
                if got:
                    await session.execute(
                        Face.__table__.update()
                        .where(Face.id == bindparam("b_id"))
                        .values(sharpness=bindparam("b_sharp")), got)
                    state["measured"] += len(got)
                state["processed"] += len(chunk)
                await session.commit()


async def shelf(session, cut: float) -> dict:
    """What is measured, and what a given cut would leave out."""
    total = (await session.execute(
        select(func.count()).select_from(Face)
        .where(Face.generation == FACE_GENERATION))).scalar_one()
    measured = (await session.execute(
        select(func.count()).select_from(Face)
        .where(Face.generation == FACE_GENERATION,
               Face.sharpness.isnot(None)))).scalar_one()
    below = (await session.execute(
        select(func.count()).select_from(Face)
        .where(Face.generation == FACE_GENERATION,
               Face.sharpness < cut))).scalar_one()
    bands = (await session.execute(text("""
        SELECT CASE WHEN sharpness < 10 THEN '0-10'
                    WHEN sharpness < 25 THEN '10-25'
                    WHEN sharpness < 50 THEN '25-50'
                    WHEN sharpness < 100 THEN '50-100'
                    WHEN sharpness < 250 THEN '100-250'
                    ELSE '250+' END AS band, count(*)
        FROM photo_faces WHERE generation = :gen AND sharpness IS NOT NULL
        GROUP BY 1
    """), {"gen": FACE_GENERATION})).all()
    order = {"0-10": 0, "10-25": 1, "25-50": 2, "50-100": 3, "100-250": 4, "250+": 5}
    return {
        "faces": total,
        "measured": measured,
        "cut": cut,
        "below_the_cut": below,
        "bands": [{"sharpness": b, "faces": n}
                  for b, n in sorted(bands, key=lambda r: order.get(r[0], 9))],
    }
