"""Pictures and map tiles, off the library's own disk.

A picture's address is its row, and the size is asked for alongside it: the
screen says how wide it draws the picture and gets the smallest kept width that
covers it."""

from fastapi import APIRouter, HTTPException, Query
from opus_core.responses import FileResponse

from opus.models.pictures import FAILED, HELD, NONE
from opus.pictures import store, tiles

router = APIRouter()

# A week, then asked again with the validator: an address stays the same
# picture, but a cover found later on the disk replaces the one first fetched.
KEEP = "public, max-age=604800"


@router.get("/pictures/{picture_id}")
async def picture(picture_id: int, w: int = Query(320, ge=1, le=4096)):
    found = await store.hold(picture_id)
    if found is None:
        raise HTTPException(404, "no such picture")
    if found.state == NONE:
        raise HTTPException(404, "the source has no picture")
    if found.state == FAILED:
        raise HTTPException(503, "the source did not answer; asked again later")
    if found.state != HELD:
        raise HTTPException(503, "not taken yet")
    path, kind = await store.sized(found, w)
    return FileResponse(path, media_type=kind, headers={"Cache-Control": KEEP})


@router.get("/tiles/{z}/{x}/{y}.png")
async def tile(z: int, x: int, y: int):
    if not tiles.valid(z, x, y):
        raise HTTPException(404, "no such tile")
    try:
        path = await tiles.tile(z, x, y)
    except tiles.NoTile as why:
        raise HTTPException(502, f"the map's tile server did not answer: {why}") from None
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": KEEP})
