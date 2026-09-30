"""A background pass: one run at a time inside this process, its progress
readable while it runs, and an ask to stop that it checks between steps."""

import asyncio
import copy
import datetime
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

# the event loop holds only weak references to its tasks
_held: set[asyncio.Task] = set()


def spawn(work: Awaitable) -> asyncio.Task:
    task = asyncio.ensure_future(work)
    _held.add(task)
    task.add_done_callback(_held.discard)
    return task


class Refused(Exception):
    """A pass that will not start its work, and says why."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


class Pass:
    """`timed` passes carry phase, error, started_at and finished_at beside their
    own counters; the others carry only `running` and their counters."""

    def __init__(self, name: str, *, timed: bool = True, **counters):
        self.name = name
        self.timed = timed
        self._fresh = counters
        self.stopping = False
        self._task: asyncio.Task | None = None
        self.state: dict = {"running": False, **copy.deepcopy(counters)}
        if timed:
            self.state.update(phase="idle", error="", started_at=None, finished_at=None)

    def _reset(self) -> dict:
        fresh = copy.deepcopy(self._fresh)
        if self.timed:
            fresh.update(phase="starting", error="", started_at=_now(), finished_at=None)
        return fresh

    @property
    def running(self) -> bool:
        return self.state["running"]

    def start(self, work: Callable[..., Awaitable], *args, **kwargs) -> bool:
        if self.running:
            return False
        self.stopping = False
        self.state.update(running=True, **self._reset())
        self._task = spawn(self._run(work(*args, **kwargs)))
        return True

    async def run(self, work: Callable[..., Awaitable], *args, **kwargs) -> dict:
        """Start it, or join the run already going, and wait for the end."""
        self.start(work, *args, **kwargs)
        if self._task is not None:
            await asyncio.wait({self._task})
        return self.state

    def cancel(self) -> bool:
        if not self.running:
            return False
        self.stopping = True
        return True

    async def _run(self, work: Awaitable) -> None:
        try:
            await work
            if self.timed:
                self.state["phase"] = "cancelled" if self.stopping else "done"
        except Refused as why:
            log.warning("%s refused: %s", self.name, why)
            if self.timed:
                self.state.update(phase="refused", error=str(why))
        except Exception as exc:
            log.exception("%s failed", self.name)
            if self.timed:
                self.state.update(phase="error", error=str(exc))
        finally:
            self.state["running"] = False
            if "current" in self._fresh:
                self.state["current"] = copy.deepcopy(self._fresh["current"])
            if self.timed:
                self.state["finished_at"] = _now()
