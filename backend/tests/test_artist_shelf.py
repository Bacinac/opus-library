from conftest import library, run, signed_in
from opus import db
from opus.models import Artist


async def _shelf(query: str) -> list[str]:
    async with db.SessionLocal() as session:
        session.add_all([Artist(id=1, name="Azra", monitored=True),
                         Artist(id=2, name="Bijelo Dugme", monitored=True),
                         Artist(id=3, name="Šarlo Akrobata", monitored=True),
                         Artist(id=4, name="Haustor", monitored=False)])
        await session.commit()
    async with library(await signed_in("boss")) as client:
        got = await client.get(f"/api/music/artists{query}")
    assert got.status_code == 200
    return [a["name"] for a in got.json()]


def test_the_whole_shelf_is_everybody_followed(clean):
    assert run(_shelf("")) == ["Azra", "Bijelo Dugme", "Šarlo Akrobata"]


def test_only_the_artists_named(clean):
    assert run(_shelf("?ids=3,4,1,x")) == ["Azra", "Šarlo Akrobata"]


def test_naming_nobody_is_nobody(clean):
    assert run(_shelf("?ids=")) == []
