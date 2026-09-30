"""Artwork candidates for either entity that has them, artists and releases,
behind one /{entity}/{entity_id}/artwork shape. A unit because the gather,
list and choose flow is identical for both and only the entity word differs —
which is why this router registers last: its two-segment prefix is a
catch-all and concrete paths must be matched first."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from opus.api.routers.music.shared import _artwork_entity
from opus.db import get_session
from opus.music.metadata import artwork

router = APIRouter()


@router.post("/{entity}/{entity_id}/artwork/refresh")
async def refresh_artwork(entity: str, entity_id: int,
                          session: AsyncSession = Depends(get_session)):
    entity_type = _artwork_entity(entity)
    try:
        if entity_type == "release":
            await artwork.refresh_release_artwork(entity_id)
        else:
            await artwork.refresh_artist_artwork(entity_id)
    except artwork.ArtworkError as exc:
        raise HTTPException(404, str(exc))
    return await artwork.list_candidates(session, entity_type, entity_id)


@router.get("/{entity}/{entity_id}/artwork")
async def get_artwork(entity: str, entity_id: int,
                      session: AsyncSession = Depends(get_session)):
    return await artwork.list_candidates(session, _artwork_entity(entity), entity_id)


@router.put("/{entity}/{entity_id}/artwork/{image_id}")
async def choose_artwork(entity: str, entity_id: int, image_id: int,
                         session: AsyncSession = Depends(get_session)):
    try:
        image = await artwork.choose(session, _artwork_entity(entity), entity_id, image_id)
    except artwork.ArtworkError as exc:
        raise HTTPException(404, str(exc))
    return {"id": image.id, "url": image.url}
