"""Radio stations, as the shelf lists them."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opus.db import get_session
from opus.models import RadioStation

router = APIRouter()


def _station(s: RadioStation) -> dict:
    return {"id": s.id, "name": s.name, "genre": s.genre, "country": s.country,
            "url": s.url, "logo": s.logo}


@router.get("/radio/stations")
async def stations(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(RadioStation).order_by(RadioStation.sort_order, RadioStation.id))).scalars()
    return {"stations": [_station(s) for s in rows]}


@router.get("/radio/stations/{station_id}")
async def station(station_id: int, session: AsyncSession = Depends(get_session)):
    found = await session.get(RadioStation, station_id)
    if found is None:
        raise HTTPException(404, "no such station")
    return _station(found)
