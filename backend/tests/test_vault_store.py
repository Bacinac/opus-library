import asyncio
import base64
import hashlib
import hmac
import json
import os
import time

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from conftest import library, run, signed_in
from opus import accounts, db
from opus.api.routers.photos import vault as router
from opus.models import Setting, VaultFile
from opus.photos import room
from opus.photos.vault import opening, store
from opus.settings_store import RuntimeConfig, forget_runtime


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _seal(key: bytes, plain: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plain, None)


def _open(key: bytes, sealed: bytes) -> bytes:
    return AESGCM(key).decrypt(sealed[:12], sealed[12:], None)


def _piece(key: bytes, base: bytes, number: int, plain: bytes) -> bytes:
    return AESGCM(key).encrypt(base + number.to_bytes(4, "big"), plain, None)


def _opened_piece(key: bytes, base: bytes, number: int, sealed: bytes) -> bytes:
    return AESGCM(key).decrypt(base + number.to_bytes(4, "big"), sealed, None)


@pytest.fixture
def config(tmp_path):
    return RuntimeConfig({"photos_vault_dir": str(tmp_path / "vault")})


def test_a_name_that_would_leave_its_own_corner_is_refused(config):
    for name in ("", "a/b", "../jana", ".hidden"):
        with pytest.raises(ValueError):
            store.theirs(config, name)
    assert store.where(config, "jana", "f1") == store.root(config) / "jana" / "f1"


def test_pieces_are_added_only_where_the_file_ends(config):
    assert store.held(config, "jana", "f1") == 0
    assert store.append(config, "jana", "f1", 0, b"abcd") == 4
    with pytest.raises(ValueError, match="starts at 2, file ends at 4"):
        store.append(config, "jana", "f1", 2, b"xx")
    with pytest.raises(ValueError, match="starts at 9, file ends at 4"):
        store.append(config, "jana", "f1", 9, b"xx")
    assert store.append(config, "jana", "f1", 4, b"ef") == 6
    assert store.where(config, "jana", "f1").read_bytes() == b"abcdef"
    assert store.held(config, "jana", "f1") == 6


def test_discarding_removes_the_partial_file_and_forgetting_the_thumbnail_too(config):
    store.append(config, "jana", "f1", 0, b"part")
    store.discard(config, "jana", "f1")
    store.discard(config, "jana", "f1")
    assert store.held(config, "jana", "f1") == 0

    store.append(config, "jana", "f2", 0, b"whole")
    store.put_thumb(config, "jana", "f2", b"thumb")
    store.forget(config, "jana", "f2")
    assert list(store.theirs(config, "jana").iterdir()) == []


def test_room_is_what_is_left_after_the_reserve(config, monkeypatch):
    monkeypatch.setattr(room, "KEEP_FREE", 0)
    assert store.room_for(config, 1)
    monkeypatch.setattr(room, "KEEP_FREE", store.spare(config) + 1)
    assert not store.room_for(config, 1)


@pytest.fixture
def vault(tmp_path, monkeypatch, quick):
    monkeypatch.setattr(room, "KEEP_FREE", 0)
    root = tmp_path / "vault"

    async def configure():
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key="photos_vault_dir", value=str(root)))
            await session.commit()
        forget_runtime()
        return await signed_in("jana", role="user"), await signed_in("filip", role="user")

    jana, filip = run(configure())
    return root, jana, filip


def reservation(size: int, chunk: int = 4, mark: bytes = b"m" * 32) -> dict:
    return {"mark": b64(mark), "bytes": size, "chunk": chunk,
            "keyed": b64(b"k" * 40), "meta": b64(b"meta")}


async def _row(file_id: str) -> VaultFile | None:
    async with db.SessionLocal() as session:
        return await session.scalar(select(VaultFile).where(VaultFile.id == file_id))


def test_a_file_is_sent_piece_by_piece_and_only_in_order(vault):
    root, jana, _ = vault
    piece = opening.TAG + 4

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(2 * piece))).json()
            url = f"/api/photos/vault/{made['id']}"
            early = await client.get(f"{url}/bytes")
            ahead = await client.put(url, params={"at": piece}, content=b"b" * piece)
            first = await client.put(url, params={"at": 0}, content=b"a" * piece)
            again = await client.put(url, params={"at": 0}, content=b"a" * piece)
            past = await client.put(url, params={"at": piece}, content=b"b" * (piece + 1))
            second = await client.put(url, params={"at": piece}, content=b"b" * piece)
            whole = await client.get(f"{url}/bytes")
            peek = await client.head(url)
        return made, early, ahead, first, again, past, second, whole, peek, await _row(made["id"])

    made, early, ahead, first, again, past, second, whole, peek, row = run(scenario())
    assert (made["at"], made["known"]) == (0, False)
    assert early.status_code == 409
    assert ahead.status_code == 409
    assert first.json() == {"at": piece, "bytes": 2 * piece, "complete": False}
    assert again.status_code == 409
    assert past.status_code == 413
    assert second.json() == {"at": 2 * piece, "bytes": 2 * piece, "complete": True}
    assert whole.content == b"a" * piece + b"b" * piece
    assert (peek.headers["x-vault-at"], peek.headers["x-vault-bytes"]) == (str(2 * piece), str(2 * piece))
    assert row.at == 2 * piece
    assert (root / row.person / made["id"]).stat().st_size == 2 * piece


def test_a_piece_past_the_announced_size_is_refused(vault):
    _, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(10, chunk=8))).json()
            url = f"/api/photos/vault/{made['id']}"
            over = await client.put(url, params={"at": 0}, content=b"x" * 11)
            empty = await client.put(url, params={"at": 0}, content=b"")
        return over, empty, await _row(made["id"])

    over, empty, row = run(scenario())
    assert over.status_code == 400 and empty.status_code == 400
    assert row.at == 0


def test_a_reservation_that_cannot_be_a_file_is_refused(vault, monkeypatch):
    _, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            answers = [await client.post("/api/photos/vault", json=body) for body in (
                reservation(0), reservation(10, chunk=0), reservation(10, chunk=store.CHUNK + 1),
                {**reservation(10), "mark": "not base64!"}, reservation(room.LARGEST + 1))]
            monkeypatch.setattr(room, "KEEP_FREE", 2 ** 62)
            answers.append(await client.post("/api/photos/vault", json=reservation(10)))
        return [r.status_code for r in answers]

    assert run(scenario()) == [400, 400, 400, 400, 413, 507]


def test_one_writer_at_a_time_per_file(vault, monkeypatch):
    root, jana, _ = vault
    append = store.append

    def slow(*args):
        time.sleep(0.2)
        return append(*args)

    monkeypatch.setattr(store, "append", slow)

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(40, chunk=20))).json()
            url = f"/api/photos/vault/{made['id']}"
            both = await asyncio.gather(
                client.put(url, params={"at": 0}, content=b"1" * 20),
                client.put(url, params={"at": 0}, content=b"2" * 20))
        return made, both

    made, both = run(scenario())
    assert sorted(r.status_code for r in both) == [200, 409]
    assert [r.json()["detail"] for r in both if r.status_code == 409] == [
        "a piece of this file is already being written"]
    owner = next(root.iterdir())
    assert (owner / made["id"]).read_bytes() in (b"1" * 20, b"2" * 20)
    assert router._writing == set()


def test_announcing_a_file_again_resumes_from_what_the_disk_holds(vault):
    root, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(12))).json()
            await client.put(f"/api/photos/vault/{made['id']}", params={"at": 0}, content=b"abcd")
            async with db.SessionLocal() as session:
                row = await session.get(VaultFile, made["id"])
                row.at = 0
                await session.commit()
            resumed = (await client.post("/api/photos/vault", json=reservation(12))).json()
            (next(root.iterdir()) / made["id"]).write_bytes(b"z" * 13)
            overgrown = (await client.post("/api/photos/vault", json=reservation(12))).json()
        return made, resumed, overgrown, await _row(made["id"])

    made, resumed, overgrown, row = run(scenario())
    for answer, offset in ((resumed, 4), (overgrown, 0)):
        assert answer["at"] == offset and answer["known"] is True
        assert {key: answer[key] for key in ("id", "bytes", "chunk", "keyed", "meta")} == {
            key: made[key] for key in ("id", "bytes", "chunk", "keyed", "meta")}
    assert not (root / row.person / made["id"]).exists()
    assert row.at == 0


def test_a_resumed_upload_keeps_its_original_encryption_material(vault):
    _, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            first = reservation(12)
            made = (await client.post("/api/photos/vault", json=first)).json()
            changed = {**reservation(99, chunk=8), "mark": first["mark"],
                       "keyed": b64(b"z" * 40), "meta": b64(b"different")}
            resumed = (await client.post("/api/photos/vault", json=changed)).json()
        return made, resumed

    made, resumed = run(scenario())
    assert resumed["known"] is True
    assert {key: resumed[key] for key in ("id", "bytes", "chunk", "keyed", "meta")} == {
        key: made[key] for key in ("id", "bytes", "chunk", "keyed", "meta")}


def test_an_interrupted_encrypted_upload_restores_through_the_real_vault_api(vault):
    """The browser and API halves agree on a resume, not just their isolated
    helpers. The first attempt is cut after one encrypted piece; its retry
    deliberately announces fresh cryptographic material, which the server must
    ignore in favour of the reservation it already owns."""
    _, jana, _ = vault
    vault_key = os.urandom(32)
    plain = b"a recovery drill must return every source byte exactly"
    chunk = 16
    mark = hmac.new(vault_key, hashlib.sha256(plain).digest(), "sha256").digest()
    first_content = os.urandom(32)
    first_base = os.urandom(8)
    first_meta = json.dumps({"name": "recovery.bin", "base": b64(first_base)}).encode()
    pieces = [
        _piece(first_content, first_base, number, plain[offset:offset + chunk])
        for number, offset in enumerate(range(0, len(plain), chunk))
    ]
    first = {
        "mark": b64(mark), "bytes": sum(map(len, pieces)), "chunk": chunk,
        "keyed": b64(_seal(vault_key, first_content)), "meta": b64(_seal(vault_key, first_meta)),
    }

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=first)).json()
            url = f"/api/photos/vault/{made['id']}"
            partial = await client.put(url, params={"at": 0}, content=pieces[0])
            retry = {
                **first,
                "keyed": b64(_seal(vault_key, os.urandom(32))),
                "meta": b64(_seal(vault_key, b'{"base":"different"}')),
            }
            resumed = (await client.post("/api/photos/vault", json=retry)).json()
            at = resumed["at"]
            for piece in pieces[1:]:
                answer = await client.put(url, params={"at": at}, content=piece)
                at = answer.json()["at"]
            stored = await client.get(f"{url}/bytes")
        return made, partial, resumed, stored

    made, partial, resumed, stored = run(scenario())
    assert partial.json()["at"] == len(pieces[0])
    assert resumed["known"] is True
    assert resumed["at"] == len(pieces[0])
    assert resumed["keyed"] == made["keyed"]
    assert resumed["meta"] == made["meta"]

    restored_content = _open(vault_key, base64.b64decode(resumed["keyed"]))
    restored_meta = json.loads(_open(vault_key, base64.b64decode(resumed["meta"])))
    restored_base = base64.b64decode(restored_meta["base"])
    opened = []
    at = 0
    for number in range(len(pieces)):
        size = min(chunk + opening.TAG, len(stored.content) - at)
        opened.append(_opened_piece(restored_content, restored_base, number, stored.content[at:at + size]))
        at += size
    assert b"".join(opened) == plain


def test_a_recreated_name_cannot_reach_the_previous_owner_s_vault(vault):
    _, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(12))).json()
        async with db.SessionLocal() as session:
            old = await accounts.by_name(session, "jana")
            assert old is not None
            await accounts.remove(session, old)
        replacement = await signed_in("jana", role="user")
        async with library(jana) as client:
            old_session = await client.get("/api/photos/vault")
        async with library(replacement) as client:
            index = await client.get("/api/photos/vault")
            fresh = await client.post("/api/photos/vault", json=reservation(12))
        return made, old_session, index, fresh

    made, old_session, index, fresh = run(scenario())
    assert old_session.status_code == 401
    assert index.json() == {"files": [], "more": False}
    assert fresh.json()["known"] is False and fresh.json()["id"] != made["id"]


def test_forgetting_a_half_sent_file_removes_its_bytes_its_thumbnail_and_its_row(vault):
    root, jana, _ = vault

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(12))).json()
            url = f"/api/photos/vault/{made['id']}"
            await client.put(url, params={"at": 0}, content=b"abcd")
            await client.put(f"{url}/thumb", content=b"thumb")
            removed = await client.delete(url)
            after = await client.head(url)
        return made, removed, after, await _row(made["id"])

    made, removed, after, row = run(scenario())
    assert removed.json() == {"removed": made["id"]}
    assert after.status_code == 404
    assert row is None
    assert list(next(root.iterdir()).iterdir()) == []


def test_another_person_s_file_answers_as_if_it_did_not_exist(vault):
    root, jana, filip = vault

    async def scenario():
        async with library(jana) as client:
            made = (await client.post("/api/photos/vault", json=reservation(12))).json()
            await client.put(f"/api/photos/vault/{made['id']}", params={"at": 0}, content=b"abcd")
        url = f"/api/photos/vault/{made['id']}"
        async with library(filip) as client:
            answers = [await client.put(url, params={"at": 4}, content=b"efgh"),
                       await client.delete(url), await client.head(url)]
            same_mark = (await client.post("/api/photos/vault", json=reservation(12))).json()
        return made, answers, same_mark

    made, answers, same_mark = run(scenario())
    assert [r.status_code for r in answers] == [404, 404, 404]
    assert same_mark["known"] is False and same_mark["id"] != made["id"]
    assert (next(root.iterdir()) / made["id"]).read_bytes() == b"abcd"


def test_a_file_missing_its_last_pieces_is_not_opened(tmp_path):
    key, base, chunk = os.urandom(32), os.urandom(8), 4
    plain = b"abcdefghij"
    pieces = [_piece(key, base, n, plain[at:at + chunk])
              for n, at in enumerate(range(0, len(plain), chunk))]
    whole = b"".join(pieces)
    sealed, into = tmp_path / "sealed", tmp_path / "opened"

    sealed.write_bytes(whole)
    checksum, size = opening.open_into(sealed, into, key, base, chunk, len(whole))
    assert (into.read_bytes(), size, checksum) == (plain, 10, hashlib.sha1(plain).digest())

    sealed.write_bytes(b"".join(pieces[:2]))
    into.unlink()
    with pytest.raises(opening.NotWhole):
        opening.open_into(sealed, into, key, base, chunk, len(whole))
    assert not into.exists()
