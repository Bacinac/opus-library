import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import opus_auth
import psycopg
import pytest

os.environ.setdefault("OPUS_PLUGIN_ROOT", "/nonexistent")
DATABASE = f"opus_test_{os.getpid()}"
SERVER = os.environ["OPUS_DATABASE_URL"].rsplit("/", 1)[0]
os.environ["OPUS_DATABASE_URL"] = f"{SERVER}/{DATABASE}"

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opus import accounts, auth, db, settings_store
from opus.config import settings
from opus.photos.people import lives

ROOT = Path(__file__).resolve().parents[1]


def _maintenance():
    return psycopg.connect(f"{SERVER.replace('+psycopg', '')}/postgres", autocommit=True)


def _abandoned(name: str) -> bool:
    try:
        os.kill(int(name.rpartition("_")[2]), 0)
    except ProcessLookupError:
        return True
    except (ValueError, PermissionError):
        pass
    return False


@pytest.fixture(scope="session", autouse=True)
def database():
    with _maintenance() as conn:
        for (name,) in conn.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'opus\\_test\\_%'"):
            if name == DATABASE or _abandoned(name):
                conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        conn.execute(f"CREATE DATABASE {DATABASE}")
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    command.upgrade(config, "head")
    db.SessionLocal.configure(
        bind=create_async_engine(os.environ["OPUS_DATABASE_URL"], poolclass=NullPool))
    yield
    with _maintenance() as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DATABASE} WITH (FORCE)")


def run(coroutine):
    return asyncio.run(coroutine)


async def _empty():
    async with db.SessionLocal() as session:
        tables = (await session.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'alembic_version'"))).scalars().all()
        await session.execute(text(
            f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
        await session.commit()


@pytest.fixture
def clean():
    run(_empty())
    auth.forget_roster()
    settings_store.forget_runtime()
    accounts._failures.clear()
    lives._runs.update(key=None, answer=None)
    yield
    auth.forget_roster()
    settings_store.forget_runtime()


@asynccontextmanager
async def library(cookie: str | None = None):
    from opus.main import app

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://library",
            cookies={opus_auth.SESSION_COOKIE: cookie} if cookie else None) as client:
        yield client


async def signed_in(name: str, role: str = "admin") -> str:
    async with db.SessionLocal() as session:
        person = await accounts.add(session, name, "a-long-enough-secret", role=role)
    return opus_auth.issue(settings.session_key, person.name, person.version)


@pytest.fixture
def quick(monkeypatch, clean):
    monkeypatch.setitem(accounts.SCRYPT, "n", 2 ** 4)
    monkeypatch.setattr(accounts, "_hashing", asyncio.Semaphore(2))
