"""Finding the faces, without deciding whose they are.

The pass sends checksums to the faces service and writes back what it answers:
a box, a score and a vector for every face it found. Nothing here groups
anything or names anybody — that is the next pass, and keeping the two apart is
what lets the grouping be redone without touching the finding.

It works from the 2048 px previews rather than the originals, which is 7.9 GB
read instead of 43 and needs no HEIC decoding in the ML container. A face large
enough to matter in a family photograph is a hundred pixels across at that size;
one that is not is one nobody was going to name.

`faces_gen` on the photograph, not a count of rows. A landscape has no faces and
that is an answer — without the mark it would be re-read on every pass forever.
"""

import logging

import httpx
from sqlalchemy import delete, select

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, Face, Photo
from opus.passes import Refused
from opus.photos.library import derive
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)

# how many photographs travel in one request. The service reads its own files,
# so this is not a payload size — it is how much work it is given before it
# answers, and a batch that is too large only makes progress invisible.
BATCH = 24

async def run() -> dict:
    """Look at every derived photograph this generation has not looked at.

    A photograph looked at by an older model loses that model's faces as it is
    read again: two models' vectors in one column mean nothing together."""
    counts = {"faces": 0, "without": 0, "failed": 0}
    async with SessionLocal() as session:
        config = await current_runtime()
        url = (config.get("faces_url") or "http://faces:8099").rstrip("/")

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                health = (await client.get(f"{url}/health")).json()
            except (httpx.HTTPError, ValueError) as exc:
                raise Refused(f"the faces service at {url} did not answer: {exc}") from exc
            if not health.get("ok"):
                raise Refused(f"the faces service is up but has no models: {health}")
            log.info("faces: %s", health.get("device", "?"))

            rows = (await session.execute(
                select(Photo.id, Photo.checksum)
                .where(Photo.kind == "image",
                       Photo.derived_gen == derive.GENERATION,
                       Photo.faces_gen != FACE_GENERATION)
                .order_by(Photo.id))).all()

            for start in range(0, len(rows), BATCH):
                chunk = rows[start:start + BATCH]
                by_checksum = {c.hex(): pid for pid, c in chunk}
                try:
                    resp = await client.post(
                        f"{url}/detect",
                        json={"checksums": list(by_checksum),
                              "generation": derive.GENERATION},
                        timeout=300)
                    resp.raise_for_status()
                    answers = resp.json()["results"]
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    counts["failed"] += len(chunk)
                    log.warning("faces: batch failed: %s", exc)
                    continue

                await session.execute(delete(Face).where(
                    Face.photo_id.in_([by_checksum[a["checksum"]] for a in answers]),
                    Face.generation != FACE_GENERATION))
                for answer in answers:
                    pid = by_checksum[answer["checksum"]]
                    if answer.get("error"):
                        counts["failed"] += 1
                        log.warning("faces: %s: %s", answer["checksum"][:12], answer["error"])
                        continue
                    found = answer["faces"]
                    for f in found:
                        session.add(Face(photo_id=pid, x=f["x"], y=f["y"],
                                         w=f["w"], h=f["h"], score=f["score"],
                                         embedding=f["embedding"],
                                         apparent_age=f.get("apparent_age"),
                                         generation=FACE_GENERATION))
                    counts["faces"] += len(found)
                    if not found:
                        counts["without"] += 1
                    await session.execute(
                        Photo.__table__.update()
                        .where(Photo.id == pid)
                        .values(faces_gen=FACE_GENERATION))
                await session.commit()
    return counts
