import asyncio
import datetime

from sqlalchemy import update

from conftest import run
from opus import db, devices
from opus.models import Device


async def _asked(module: str = "player") -> tuple[int, str, str]:
    async with db.SessionLocal() as session:
        device, claim = await devices.ask(session, module)
        return device.id, device.code, claim


async def _let_in(code: str):
    async with db.SessionLocal() as session:
        device = await devices.waiting(session, code.lower().replace("-", " "))
        assert device is not None
        await devices.let_in(session, device, "living room", "boss")


async def _collect(device_id: int, claim: str):
    async with db.SessionLocal() as session:
        return await devices.collect(session, device_id, claim)


def test_a_code_is_worth_nothing_until_somebody_says_yes(clean):
    async def scenario():
        device_id, code, claim = await _asked()
        assert set(code.replace("-", "")) <= set(devices.ALPHABET)
        assert await _collect(device_id, claim) == {"in": False, "token": None, "name": ""}
        assert await _collect(device_id, "somebody-else's-claim") is None
        await _let_in(code)
        async with db.SessionLocal() as session:
            assert await devices.waiting(session, code) is None
    run(scenario())


def test_the_token_is_minted_once_for_one_caller(clean):
    async def scenario():
        device_id, code, claim = await _asked()
        await _let_in(code)
        said = await asyncio.gather(*(_collect(device_id, claim) for _ in range(8)))
        minted = [s for s in said if s is not None]
        assert len(minted) == 1
        assert minted[0]["in"] and minted[0]["name"] == "living room"
        assert await _collect(device_id, claim) is None
        async with db.SessionLocal() as session:
            device = await devices.holding(session, minted[0]["token"])
            assert device is not None and device.id == device_id
            assert device.token == devices.digest(minted[0]["token"])
            assert device.claim is None
            assert await devices.holding(session, "not-a-token") is None
    run(scenario())


def test_a_code_runs_out(clean):
    async def scenario():
        device_id, code, claim = await _asked()
        async with db.SessionLocal() as session:
            await session.execute(
                update(Device).where(Device.id == device_id)
                .values(created_at=datetime.datetime.now(datetime.UTC) - devices.GOOD_FOR
                        - datetime.timedelta(seconds=1)))
            await session.commit()
            assert await devices.waiting(session, code) is None
        assert await _collect(device_id, claim) is None
    run(scenario())


def test_taking_a_box_back_ends_its_token(clean):
    async def scenario():
        device_id, code, claim = await _asked()
        await _let_in(code)
        token = (await _collect(device_id, claim))["token"]
        async with db.SessionLocal() as session:
            await devices.take_back(session, await devices.claimed(session, device_id))
            assert await devices.holding(session, token) is None
    run(scenario())


def test_the_list_never_carries_the_code(clean):
    async def scenario():
        await _asked()
        async with db.SessionLocal() as session:
            shown = [devices.shown(d) for d in await devices.everyone(session)]
        assert shown and all("code" not in s and "claim" not in s and "token" not in s
                             for s in shown)
    run(scenario())


def test_a_box_renamed_answers_to_its_new_name(clean):
    async def scenario():
        device_id, code, claim = await _asked()
        await _let_in(code)
        token = (await _collect(device_id, claim))["token"]
        async with db.SessionLocal() as session:
            await devices.rename(session, await devices.claimed(session, device_id), "  Nika  ")
            device = await devices.holding(session, token)
            assert device is not None and device.name == "Nika"
    run(scenario())
