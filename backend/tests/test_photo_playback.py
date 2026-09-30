import hashlib
import subprocess

from conftest import library, run, signed_in
from opus import db
from opus.models import Photo, PhotoFile


def recording(tmp_path, name: str, *codec: str):
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1", *codec, str(path)], check=True)
    return path


async def catalogued(path) -> str:
    digest = hashlib.sha1(path.name.encode()).digest()
    async with db.SessionLocal() as session:
        photo = Photo(checksum=digest, kind="video")
        session.add(photo)
        await session.flush()
        session.add(PhotoFile(photo_id=photo.id, path=str(path)))
        await session.commit()
    return digest.hex()


def test_a_recording_says_what_it_is_for_a_player_to_decide(clean, tmp_path):
    divx = recording(tmp_path, "Kupanje.avi", "-c:v", "mpeg4", "-c:a", "mp3")
    broken = tmp_path / "broken.mov"
    broken.write_bytes(b"not a recording")

    async def scenario():
        cookie = await signed_in("boss")
        good, bad = await catalogued(divx), await catalogued(broken)
        async with library(cookie) as client:
            return (await client.get(f"/api/photos/{good}/playback"),
                    await client.get(f"/api/photos/{bad}/playback"),
                    await client.get(f"/api/photos/{'0' * 40}/playback"))

    said, unreadable, missing = run(scenario())
    assert said.status_code == 200
    facts = said.json()
    assert (facts["path"], facts["container"], facts["video_codec"], facts["width"], facts["height"]) == \
        (str(divx), "avi", "mpeg4", 320, 240)
    assert [(s["kind"], s["codec"], s["position"]) for s in facts["streams"]] == \
        [("video", "mpeg4", 0), ("audio", "mp3", 0)]
    assert unreadable.status_code == 422
    assert missing.status_code == 404
