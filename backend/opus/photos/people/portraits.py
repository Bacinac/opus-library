"""A person's face cut out, stood upright, and carried through the years."""

import asyncio
import hashlib
import json
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import bindparam, select, text

from opus.models import FACE_GENERATION, Face, Person
from opus.photos.library import derive

log = logging.getLogger(__name__)

CROP_PX = 256
CROP_BIG_PX = 512
# how much room around the box. A detector's box stops at the jaw, and a face
# cut exactly there is hard to recognise — the hair and the chin are most of
# what a person uses.
MARGIN = 0.45

# Where the eyes are put in the finished frame: the face is rotated until they
# are level, scaled until they are this far apart, and moved until they sit here.
EYES = (0.35, 0.42, 0.65, 0.42)
PORTRAIT_PX = 448

# How many faces of a year the morph may try before giving that year up: the
# first one offered fails often enough — a face at the edge of its photograph, a
# mesh on nothing — that trying only one loses years the archive can answer for.
CANDIDATES = 14

# Under this many pixels across, a portrait blown up to 448 is guesswork whatever
# its sharpness says, so size decides first and sharpness among the big enough.
WIDE_ENOUGH = 120

MORPH_HOLD = 3
MORPH_STEPS = 10
MORPH_MS = 60
# 256 is what the still crop beside it is cut at, so a circle that starts moving
# does not also go soft.
MORPH_SIZES = (256, 448)

# Morphs and landmarks asked for by whoever is looking run on the same card as
# the library's own passes, and one morph is eighteen seconds of it.
GPU_AT_ONCE = 2
_on_the_card = 0

FACES_URL = "http://faces:8099"


class NotFound(Exception):
    pass


class Uncuttable(Exception):
    pass


class CardBusy(Exception):
    def __init__(self):
        super().__init__("the faces service is already making as much as it can")


class FacesUnavailable(Exception):
    pass


@dataclass
class FacesRefused(Exception):
    status: int
    said: str


def crop_square(x: float, y: float, w: float, h: float,
                width: int, height: int) -> tuple[int, int, int]:
    """The square a face is cut out as, in pixels of the picture given. What is
    cut out and what is later framed on the whole photograph have to be the same
    rectangle, so both ask here."""
    cx, cy = (x + w / 2) * width, (y + h / 2) * height
    side = max(w * width, h * height) * (1 + MARGIN)
    left = int(max(0, min(width - 1, cx - side / 2)))
    top = int(max(0, min(height - 1, cy - side / 2)))
    return left, top, int(min(side, width - left, height - top))


def _kept(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(target.name + ".part")
    staged.write_bytes(data)
    staged.replace(target)


def _cut_face(preview: Path, box: tuple[float, float, float, float], px: int,
              target: Path) -> None:
    import pyvips

    image = pyvips.Image.new_from_file(str(preview))
    left, top, side = crop_square(*box, image.width, image.height)
    if side < 8:
        raise Uncuttable("the box is too small to cut")
    crop = image.crop(left, top, side, side).thumbnail_image(px)
    _kept(target, crop.write_to_buffer(".jpg[Q=82,optimize_coding,keep=icc]"))


def _upright(preview: Path, marks: list[float], target: Path) -> None:
    import pyvips

    image = pyvips.Image.new_from_file(str(preview))
    lx, ly = marks[0] * image.width, marks[1] * image.height
    rx, ry = marks[2] * image.width, marks[3] * image.height
    tlx, tly = EYES[0] * PORTRAIT_PX, EYES[1] * PORTRAIT_PX
    trx, tr_y = EYES[2] * PORTRAIT_PX, EYES[3] * PORTRAIT_PX

    have = ((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5
    want = ((trx - tlx) ** 2 + (tr_y - tly) ** 2) ** 0.5
    if have <= 0:
        raise Uncuttable("the eyes are in the same place")
    scale = want / have
    angle = math.atan2(tr_y - tly, trx - tlx) - math.atan2(ry - ly, rx - lx)
    a, b = scale * math.cos(angle), -scale * math.sin(angle)
    c, d = scale * math.sin(angle), scale * math.cos(angle)
    dx = tlx - (a * lx + b * ly)
    dy = tly - (c * lx + d * ly)

    frame = image.affine(
        [a, b, c, d], odx=dx, ody=dy, oarea=[0, 0, PORTRAIT_PX, PORTRAIT_PX],
        interpolate=pyvips.Interpolate.new("bicubic"), extend="mirror")
    _kept(target, frame.write_to_buffer(".jpg[Q=86,optimize_coding]"))


def _preview(config, checksum: str) -> Path:
    path = derive.paths_for(derive.store_root(config), bytes.fromhex(checksum))[1]
    if not path.exists():
        raise NotFound("the preview this face was found in is gone")
    return path


async def crop(session, config, face_id: int, big: bool) -> Path:
    """One face, cut out of the preview already kept, and kept itself after the
    first time: a wall of three hundred is half a minute of decoding otherwise."""
    px = CROP_BIG_PX if big else CROP_PX
    kept = (derive.store_root(config) / "crop"
            / f"{face_id % 256:02x}" / f"{face_id}-g{derive.GENERATION}-{px}.jpg")
    if kept.exists():
        return kept
    row = (await session.execute(text("""
        SELECT f.x, f.y, f.w, f.h, encode(p.checksum, 'hex')
        FROM photo_faces f JOIN photos p ON p.id = f.photo_id
        WHERE f.id = :fid
    """), {"fid": face_id})).first()
    if row is None:
        raise NotFound("no such face")
    x, y, w, h, checksum = row
    await asyncio.to_thread(_cut_face, _preview(config, checksum), (x, y, w, h), px, kept)
    return kept


async def portrait(session, config, face_id: int) -> Path:
    """One face, turned upright and framed the same as every other, so a run of
    them is a person changing rather than a face wandering around the frame."""
    kept = (derive.store_root(config) / "portrait"
            / f"{face_id % 256:02x}" / f"{face_id}-g{derive.GENERATION}.jpg")
    if kept.exists():
        return kept
    row = (await session.execute(text("""
        SELECT f.landmarks, encode(p.checksum, 'hex')
        FROM photo_faces f JOIN photos p ON p.id = f.photo_id
        WHERE f.id = :fid
    """), {"fid": face_id})).first()
    if row is None:
        raise NotFound("no such face")
    marks, checksum = row
    if not marks or len(marks) < 10:
        raise Uncuttable("this face has no landmarks yet")
    await asyncio.to_thread(_upright, _preview(config, checksum), marks, kept)
    return kept


@asynccontextmanager
async def _card():
    global _on_the_card
    if _on_the_card >= GPU_AT_ONCE:
        raise CardBusy()
    _on_the_card += 1
    try:
        yield
    finally:
        _on_the_card -= 1


async def _ask_faces(config, path: str, body: dict, timeout: float) -> httpx.Response:
    url = (config.get("faces_url") or FACES_URL).rstrip("/")
    async with _card():
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                answer = await client.post(f"{url}{path}", json=body)
        except httpx.HTTPError as exc:
            raise FacesUnavailable(f"the faces service could not be asked: {exc}") from exc
    if answer.status_code != 200:
        raise FacesRefused(answer.status_code, answer.text[:300])
    return answer


async def _made_of(session, person_id: int) -> str:
    """A fingerprint of everything the run is derived from: which faces this
    person has, and how many carry a mesh — a mesh arriving later changes which
    face a year is shown by without changing any id."""
    row = (await session.execute(text("""
        SELECT count(*), coalesce(max(id), 0), coalesce(sum(id), 0),
               count(*) FILTER (WHERE landmarks IS NOT NULL)
          FROM photo_faces WHERE person_id = :p
    """), {"p": person_id})).one()
    return hashlib.sha256(f"{CANDIDATES}:{tuple(row)}".encode()).hexdigest()[:16]


def _about(person: Person) -> dict:
    return {"id": person.id, "name": person.name,
            "born_on": person.born_on.isoformat() if person.born_on else None}


async def _year_candidates(session, person_id: int, per: int, order: str):
    # a portrait comes from a photograph: a frame cut out of a recording is a
    # face of the same person, but a soft interlaced 720x528 would be the worst
    # picture in a run otherwise made of photographs
    return (await session.execute(text(f"""
        SELECT id, year, checksum, x, y, w, h, landmarks, sharpness FROM (
            SELECT f.id, EXTRACT(YEAR FROM p.taken_at)::int AS year,
                   encode(p.checksum, 'hex') AS checksum,
                   f.x, f.y, f.w, f.h, f.landmarks, f.sharpness,
                   row_number() OVER (
                       PARTITION BY EXTRACT(YEAR FROM p.taken_at)
                       ORDER BY (f.w * COALESCE(p.pixel_w, 2048) >= :wide) DESC,
                                f.sharpness DESC NULLS LAST,
                                f.w * f.h DESC) AS rn
            FROM photo_faces f
            JOIN photos p ON p.id = f.photo_id
            WHERE f.person_id = :pid AND f.generation = :gen
              AND p.taken_at IS NOT NULL
              AND p.kind = 'image'
        ) t WHERE rn <= :per ORDER BY year, {order}
    """), {"pid": person_id, "gen": FACE_GENERATION, "per": per,
           "wide": WIDE_ENOUGH})).all()


async def _landmarks(session, config, rows) -> dict[int, list[float]]:
    marks_of = {r.id: r.landmarks for r in rows}
    missing = [r for r in rows if r.landmarks is None]
    if not missing:
        return marks_of
    answer = await _ask_faces(config, "/landmarks", {
        "faces": [{"id": r.id, "x": r.x, "y": r.y, "w": r.w, "h": r.h,
                   "checksum": r.checksum} for r in missing],
        "generation": derive.GENERATION}, timeout=120)
    try:
        found = {a["id"]: a["landmarks"] for a in answer.json()["results"]
                 if a.get("landmarks")}
    except (ValueError, KeyError, TypeError) as exc:
        raise FacesUnavailable("the faces service answered something that is not landmarks") from exc
    if found:
        await session.execute(
            Face.__table__.update()
            .where(Face.id == bindparam("b_id"))
            .values(landmarks=bindparam("b_marks")),
            [{"b_id": k, "b_marks": v} for k, v in found.items()])
        await session.commit()
        marks_of.update(found)
    return marks_of


def _judged(rows, marks_of: dict) -> list[dict]:
    """Whether each candidate looks straight ahead, how much detail it carries,
    and how far its mouth is from the person's ordinary one — not the detector's
    confidence: a confident profile is still a profile."""
    judged = []
    for r in rows:
        marks = marks_of.get(r.id)
        if not marks or len(marks) < 10:
            continue
        lx, ly, rx, ry, nx = marks[0], marks[1], marks[2], marks[3], marks[4]
        mlx, mly, mrx, mry = marks[6], marks[7], marks[8], marks[9]
        wide = ((rx - lx) ** 2 + (ry - ly) ** 2) ** 0.5
        if wide <= 0:
            continue
        turned = abs(nx - (lx + rx) / 2) / wide
        tilted = abs(ry - ly) / wide
        mouth = (((mrx - mlx) ** 2 + (mry - mly) ** 2) ** 0.5) / wide
        judged.append({"id": r.id, "year": r.year, "looking": turned + tilted,
                       "mouth": mouth, "sharp": r.sharpness or 0.0})
    return judged


def _best_per_year(judged: list[dict]) -> dict[int, tuple[float, int]]:
    usual = sorted(j["mouth"] for j in judged)[len(judged) // 2]
    keenest = max(j["sharp"] for j in judged) or 1.0
    best: dict[int, tuple[float, int]] = {}
    for j in judged:
        score = (j["looking"]
                 + 1.5 * abs(j["mouth"] - usual)
                 + 0.4 * (1 - min(j["sharp"] / keenest, 1.0)))
        if j["year"] not in best or score < best[j["year"]][0]:
            best[j["year"]] = (score, j["id"])
    return best


async def transformation(session, config, person_id: int) -> dict:
    """One portrait a year, in order: somebody growing up, or growing old."""
    person = (await session.execute(
        select(Person).where(Person.id == person_id))).scalar_one_or_none()
    if person is None:
        raise NotFound("no such person")
    kept = (derive.store_root(config) / "transformation"
            / f"{person_id}-{await _made_of(session, person_id)}.json")
    if kept.exists():
        return json.loads(kept.read_text())

    rows = await _year_candidates(session, person_id, per=6, order="id")
    if not rows:
        return {"person": {"id": person.id, "name": person.name}, "frames": []}
    judged = _judged(rows, await _landmarks(session, config, rows))
    if not judged:
        return {"person": _about(person), "frames": []}
    run = {
        "person": _about(person),
        "frames": [{"year": year, "face": fid, "straightness": round(1 - min(score, 1), 3)}
                   for year, (score, fid) in sorted(_best_per_year(judged).items())],
        "morph": {"ms": MORPH_MS, "hold": MORPH_HOLD, "steps": MORPH_STEPS},
    }
    _kept(kept, json.dumps(run).encode())
    return run


async def morph(session, config, person_id: int, size: int, steps: int) -> Path:
    """The run of portraits as one face becoming the next, made once on the card
    and kept under a name that carries the faces it was made of."""
    frames = (await transformation(session, config, person_id))["frames"]
    if len(frames) < 2:
        raise Uncuttable("two years at least are needed to become one another")
    made_of = hashlib.sha256(
        (f"{CANDIDATES}:" + ",".join(str(f["face"]) for f in frames)).encode()
    ).hexdigest()[:16]
    kept = derive.store_root(config) / "morphs" / f"{person_id}-{made_of}-{size}-{steps}.webp"
    if kept.exists():
        return kept

    rows = await _year_candidates(session, person_id, per=CANDIDATES, order="rn")
    if not rows:
        raise Uncuttable("no faces to become one another")
    answer = await _ask_faces(config, "/morph", {
        "faces": [{"id": r.id, "year": r.year, "x": r.x, "y": r.y,
                   "w": r.w, "h": r.h, "checksum": r.checksum} for r in rows],
        "generation": derive.GENERATION,
        "steps": steps, "ms": MORPH_MS, "hold": MORPH_HOLD, "size": size}, timeout=600)
    _kept(kept, answer.content)
    log.info("morph for person %s: %s years, %s", person_id,
             answer.headers.get("X-OPUS-Frames", ""), answer.headers.get("X-OPUS-Years", ""))
    return kept


def forget_morphs(config, people: set[int]) -> int:
    """Every morph made of these people, thrown away: a name nobody asks for is
    not a file that is gone, and somebody who just changed a person should not
    have to wonder whether the reel is the one they changed."""
    folder = derive.store_root(config) / "morphs"
    if not folder.exists():
        return 0
    gone = 0
    for person_id in people:
        for kept in folder.glob(f"{person_id}-*.webp"):
            kept.unlink(missing_ok=True)
            gone += 1
    return gone
