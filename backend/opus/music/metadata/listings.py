"""An artist's releases as a catalogue pages them, read once a week."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert

from opus.models import CatalogListing

LISTING_DAYS = 7


async def cached(session, source: str, artist_key, fetch: Callable[[], Awaitable[list]]) -> list:
    """The listing as last read, or a fresh read once it is a week old. A read
    that raises stores nothing, so a source in a ban is asked again later."""
    now = datetime.now(timezone.utc)
    row = await session.get(CatalogListing, (source, str(artist_key)))
    if row is not None and row.fetched_at > now - timedelta(days=LISTING_DAYS):
        return row.items
    items = await fetch()
    await session.execute(
        insert(CatalogListing)
        .values(source=source, artist_key=str(artist_key), items=items, fetched_at=now)
        .on_conflict_do_update(index_elements=[CatalogListing.source, CatalogListing.artist_key],
                               set_={"items": items, "fetched_at": now}))
    return items


async def store(session, source: str, artist_key, items: list) -> None:
    """What a pass learned about the listing's entries, written back without
    making the listing any younger."""
    await session.execute(
        insert(CatalogListing)
        .values(source=source, artist_key=str(artist_key), items=items)
        .on_conflict_do_update(index_elements=[CatalogListing.source, CatalogListing.artist_key],
                               set_={"items": items}))
