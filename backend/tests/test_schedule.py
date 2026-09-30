import asyncio
import datetime
from types import SimpleNamespace

from conftest import run
from opus import db, schedule
from opus.models import LoopPass

SIX_HOURS = datetime.timedelta(hours=6)


async def _ran_ago(name: str, ago: datetime.timedelta) -> None:
    async with db.SessionLocal() as session:
        (await session.get(LoopPass, name)).finished_at = datetime.datetime.now(datetime.UTC) - ago
        await session.commit()


def test_a_restart_neither_brings_a_pass_forward_nor_puts_it_back(clean):
    async def scenario():
        never = await schedule.due_in("probe", SIX_HOURS)
        await schedule.finished("probe")
        just_ran = await schedule.due_in("probe", SIX_HOURS)
        await _ran_ago("probe", datetime.timedelta(hours=5))
        hour_left = await schedule.due_in("probe", SIX_HOURS)
        await _ran_ago("probe", datetime.timedelta(hours=7))
        overdue = await schedule.due_in("probe", SIX_HOURS)
        return never, just_ran, hour_left, overdue

    never, just_ran, hour_left, overdue = run(scenario())
    assert never == 0
    assert SIX_HOURS.total_seconds() - 60 < just_ran <= SIX_HOURS.total_seconds()
    assert 3600 - 60 < hour_left <= 3600
    assert overdue == 0


def test_a_pass_that_fails_is_tried_again_and_not_counted_as_run(clean, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError

    async def broken():
        raise RuntimeError("indexer down")

    monkeypatch.setattr(schedule, "asyncio", SimpleNamespace(sleep=sleep))

    async def scenario():
        try:
            await schedule.every("probe", SIX_HOURS, broken)
        except asyncio.CancelledError:
            pass
        async with db.SessionLocal() as session:
            return await session.get(LoopPass, "probe")

    kept = run(scenario())
    assert waits == [0, SIX_HOURS.total_seconds()]
    assert kept is None
