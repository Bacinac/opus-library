"""What a guest with a circle sees of the album: the photographs somebody of
the circle is in, and nothing else of it.

The guard lets such a guest reach only the routes that narrow by this, so a
route that does not is a route they never get to."""

from fastapi import HTTPException, Request
from sqlalchemy import select

from opus.models import Face, Photo


def of(request: Request) -> tuple[int, ...] | None:
    """The people whose photographs this request may see, or None for all."""
    return getattr(request.state, "circle", None)


def shows(circle: tuple[int, ...]):
    """The photograph somebody of the circle is in."""
    return select(Face.id).where(Face.photo_id == Photo.id, Face.person_id.in_(circle)).exists()


async def sees(session, photo_id: int, circle: tuple[int, ...] | None) -> None:
    """A photograph outside the circle is one that does not exist."""
    if circle is None:
        return
    found = await session.scalar(select(Face.id).where(
        Face.photo_id == photo_id, Face.person_id.in_(circle)).limit(1))
    if found is None:
        raise HTTPException(404, "no such photograph")


async def holds(session, digest: bytes, circle: tuple[int, ...] | None) -> None:
    """The same, for a photograph named by its checksum."""
    if circle is None:
        return
    found = await session.scalar(select(Photo.id).where(Photo.checksum == digest, shows(circle)))
    if found is None:
        raise HTTPException(404, "no such photograph")


async def face(session, face_id: int, circle: tuple[int, ...] | None) -> None:
    """A face is seen where the photograph it is cut from is."""
    if circle is None:
        return
    photo = await session.scalar(select(Face.photo_id).where(Face.id == face_id))
    if photo is None:
        raise HTTPException(404, "no such face")
    await sees(session, photo, circle)


def person(person_id: int, circle: tuple[int, ...] | None) -> None:
    if circle is not None and person_id not in circle:
        raise HTTPException(404, "no such person")
