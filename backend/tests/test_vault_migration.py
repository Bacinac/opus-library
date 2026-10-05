import os
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from conftest import SERVER, run
from opus.api.routers.photos import vault
from opus.models import Setting, User, VaultFile
from opus.photos import room
from opus.photos.vault import migrate, store
from opus.settings_store import RuntimeConfig

OLD = "a" * 32
NEW = "b" * 32
FILE = "c" * 32


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def write(root, folder, name, data):
    directory = root / folder
    directory.mkdir(exist_ok=True)
    (directory / name).write_bytes(data)


def test_reused_names_do_not_transfer_an_old_accounts_bytes(root):
    write(root, "reused", FILE, b"old account")
    write(root, "reused", "untracked", b"unclaimed")
    assert migrate.relocate(root, {FILE: OLD}) == 1
    assert (root / OLD / FILE).read_bytes() == b"old account"
    assert not (root / NEW / FILE).exists()
    assert (root / "reused" / "untracked").read_bytes() == b"unclaimed"
    assert migrate.relocate(root, {FILE: OLD}) == 0


def test_conflicting_objects_are_both_preserved(root):
    write(root, "legacy", FILE, b"original")
    write(root, OLD, FILE, b"different")
    with pytest.raises(RuntimeError, match="conflicting vault objects"):
        migrate.relocate(root, {FILE: OLD})
    assert (root / "legacy" / FILE).read_bytes() == b"original"
    assert (root / OLD / FILE).read_bytes() == b"different"


@pytest.mark.parametrize(("source", "target"), [(b"complete", b"comp"), (b"comp", b"complete"),
                                             (b"complete", b"complete")])
def test_a_partial_duplicate_is_consolidated_without_losing_any_bytes(root, source, target):
    write(root, "legacy", FILE, source)
    write(root, OLD, FILE, target)
    assert migrate.relocate(root, {FILE: OLD}) == 1
    assert (root / OLD / FILE).read_bytes() == b"complete"
    assert not (root / "legacy").exists()


@pytest.mark.parametrize("folder", ["legacy", OLD])
def test_a_linked_object_cannot_escape_storage(root, tmp_path, folder):
    outside = tmp_path / "private"
    outside.write_bytes(b"outside")
    legacy = root / folder
    legacy.mkdir()
    (legacy / FILE).symlink_to(outside)
    with pytest.raises(RuntimeError, match="not a regular file"):
        migrate.relocate(root, {FILE: OLD})
    assert outside.read_bytes() == b"outside"


def test_a_linked_destination_is_refused_before_moving_anything(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    owner = "f" * 32
    (root / owner).symlink_to(outside, target_is_directory=True)
    write(root, "a-legacy", FILE, b"original")
    with pytest.raises(RuntimeError, match="linked directory"):
        migrate.relocate(root, {FILE: owner})
    assert (root / "a-legacy" / FILE).read_bytes() == b"original"
    assert list(outside.iterdir()) == []


@pytest.fixture
def upgraded(root):
    database = f"opus_vault_upgrade_test_{os.getpid()}"
    dsn = SERVER.replace("+psycopg", "")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    with psycopg.connect(f"{dsn}/postgres", autocommit=True) as maintenance:
        maintenance.execute(f"CREATE DATABASE {database}")
    from opus.config import settings

    original = settings.database_url
    settings.database_url = f"{SERVER}/{database}"
    try:
        command.upgrade(config, "21405765a70f")
        with psycopg.connect(f"{dsn}/{database}") as conn:
            conn.execute("INSERT INTO users (name, display, secret, version, role, disabled) "
                         "VALUES ('legacy', '', 'synthetic', 1, 'user', false)")
            conn.execute("INSERT INTO vault_files "
                         "(id, person, mark, bytes, at, chunk, keyed, meta) "
                         "VALUES (%s, 'legacy', %s, 8, 0, 4, %s, %s)",
                         (FILE, b"m" * 32, b"wrapped", b"metadata"))
        write(root, "legacy", FILE, b"complete")
        write(root, "legacy", FILE + ".t", b"thumbnail")
        command.upgrade(config, "head")
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        yield RuntimeConfig({"photos_vault_dir": str(root)}), sessions
        run(engine.dispose())
    finally:
        settings.database_url = original
        with psycopg.connect(f"{dsn}/postgres", autocommit=True) as maintenance:
            maintenance.execute(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")


@pytest.mark.parametrize("interrupted", [False, True])
def test_a_populated_upgrade_restores_originals_thumbnails_and_resume(upgraded, root, monkeypatch, interrupted):
    config, sessions = upgraded
    original_move = migrate._move

    def stop_after_one_object(source, target):
        original_move(source, target)
        raise OSError("simulated interruption after storage relocation")

    async def runtime():
        return config

    monkeypatch.setattr(vault, "current_runtime", runtime)
    monkeypatch.setattr(room, "KEEP_FREE", 0)

    async def scenario():
        async with sessions() as session:
            owner = await session.scalar(select(User.identity).where(User.name == "legacy"))
            assert await session.scalar(select(VaultFile.person)) == owner
        if interrupted:
            monkeypatch.setattr(migrate, "_move", stop_after_one_object)
            with pytest.raises(OSError, match="simulated interruption"):
                await migrate.migrate(config, sessions)
            async with sessions() as session:
                assert await session.get(Setting, migrate.VERSION_KEY) is None
            monkeypatch.setattr(migrate, "_move", original_move)
        await migrate.migrate(config, sessions)
        assert store.where(config, owner, FILE).read_bytes() == b"complete"
        assert store.thumb_at(config, owner, FILE).read_bytes() == b"thumbnail"
        assert not (root / "legacy").exists()
        assert await migrate.migrate(config, sessions) == 0
        async with sessions() as session:
            assert await session.scalar(select(VaultFile.at)) == 8
            body = vault.Reservation(mark="bW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW0=",
                                     bytes=8, chunk=4, keyed="d3JhcHBlZA==", meta="bWV0YWRhdGE=")
            answer = await vault.reserve(body, owner, session)
            assert answer["id"] == FILE and answer["known"] and answer["at"] == 8
            assert answer["keyed"] == "d3JhcHBlZA==" and answer["meta"] == "bWV0YWRhdGE="

    run(scenario())
