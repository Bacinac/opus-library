import asyncio

from conftest import run
from opus import passes


def test_one_run_at_a_time_and_its_counters_reset():
    job = passes.Pass("probe", done=0, current="")

    async def work(gate: asyncio.Event):
        job.state["current"] = "somewhere"
        await gate.wait()
        job.state["done"] += 1

    async def scenario():
        gate = asyncio.Event()
        assert job.start(work, gate)
        assert job.running and job.state["phase"] == "starting"
        assert not job.start(work, gate)
        joined = asyncio.ensure_future(job.run(work, gate))
        await asyncio.sleep(0)
        gate.set()
        state = await joined
        assert state["done"] == 1 and state["phase"] == "done"
        assert not job.running and state["finished_at"] and state["current"] == ""

        gate.set()
        await job.run(work, gate)
        assert job.state["done"] == 1
    run(scenario())


def test_a_refusal_and_a_failure_are_said():
    job = passes.Pass("probe")

    async def refuse():
        raise passes.Refused("the mount is empty")

    async def fail():
        raise RuntimeError("broken")

    async def scenario():
        state = await job.run(refuse)
        assert (state["phase"], state["error"]) == ("refused", "the mount is empty")
        state = await job.run(fail)
        assert (state["phase"], state["error"]) == ("error", "broken")
        assert not job.running
    run(scenario())


def test_asking_to_stop_is_checked_by_the_work():
    job = passes.Pass("probe", steps=0)

    async def work():
        while not job.stopping:
            job.state["steps"] += 1
            await asyncio.sleep(0)

    async def scenario():
        assert not job.cancel()
        job.start(work)
        await asyncio.sleep(0.01)
        assert job.cancel()
        await job.run(work)
        assert job.state["phase"] == "cancelled" and job.state["steps"] > 0
    run(scenario())


def test_an_untimed_pass_carries_only_its_counters():
    job = passes.Pass("probe", timed=False, seen=0)

    async def work():
        job.state["seen"] = 3

    async def scenario():
        state = await job.run(work)
        assert state == {"running": False, "seen": 3}
    run(scenario())


def test_a_spawned_task_is_held_until_it_ends():
    async def scenario():
        gate = asyncio.Event()
        task = passes.spawn(gate.wait())
        assert task in passes._held
        gate.set()
        await task
        await asyncio.sleep(0)
        assert task not in passes._held
    run(scenario())
