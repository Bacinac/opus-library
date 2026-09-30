from fastapi import Depends, HTTPException

from opus.db import get_session
from opus.photos.people import cluster


async def unmoved(session=Depends(get_session)) -> None:
    try:
        await cluster.unmoved(session)
    except cluster.Regrouping:
        raise HTTPException(409, {"reason": "regrouping"})
