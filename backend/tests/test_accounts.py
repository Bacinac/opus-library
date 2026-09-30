import asyncio
import threading
from types import SimpleNamespace

import pytest

from conftest import run
import opus_auth

from opus import accounts, auth, db
from opus.config import settings

RIGHT = "the-right-password"


async def _person(name: str):
    async with db.SessionLocal() as session:
        await accounts.add(session, name, RIGHT)


async def _check(name: str, secret: str, address: str):
    async with db.SessionLocal() as session:
        return await accounts.check(session, name, secret, address)


async def _wrong(name: str, address: str, times: int):
    for _ in range(times):
        assert await _check(name, "wrong", address) is None


def test_the_right_password_signs_in(quick):
    async def scenario():
        await _person("Filip")
        person = await _check(" FILIP ", RIGHT, "10.0.0.5")
        assert person is not None and person.name == "filip"
        assert await _check("filip", "wrong", "10.0.0.5") is None
        assert await _check("nobody", RIGHT, "10.0.0.5") is None
    run(scenario())


def test_a_name_from_one_address_is_bounded(quick):
    async def scenario():
        await _person("filip")
        await _wrong("filip", "203.0.113.9", accounts.FAILS_PER_NAME_FROM_ADDRESS)
        with pytest.raises(accounts.TooManyAttempts) as refused:
            await _check("filip", RIGHT, "203.0.113.9")
        assert 0 < refused.value.retry_after <= accounts.WINDOW + 1
        person = await _check("filip", RIGHT, "192.168.1.20")
        assert person is not None and person.name == "filip"
    run(scenario())


def test_an_address_is_bounded_across_names(quick):
    async def scenario():
        await _person("filip")
        for n in range(accounts.FAILS_PER_ADDRESS):
            await _wrong(f"guess{n % 3}", "198.51.100.7", 1)
        with pytest.raises(accounts.TooManyAttempts):
            await _check("filip", RIGHT, "198.51.100.7")
        assert await _check("filip", RIGHT, "192.168.1.20") is not None
    run(scenario())


def test_a_stranger_cannot_lock_somebody_out(quick):
    async def scenario():
        await _person("filip")
        for n in range(5):
            await _wrong("filip", f"203.0.113.{n}", accounts.FAILS_PER_NAME_FROM_ADDRESS)
        assert await _check("filip", RIGHT, "192.168.1.20") is not None
    run(scenario())


def test_the_window_forgets(quick, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(accounts, "time", SimpleNamespace(monotonic=lambda: now[0]))

    async def scenario():
        await _person("filip")
        await _wrong("filip", "203.0.113.9", accounts.FAILS_PER_NAME_FROM_ADDRESS)
        with pytest.raises(accounts.TooManyAttempts):
            await _check("filip", RIGHT, "203.0.113.9")
        now[0] += accounts.WINDOW + 1
        assert await _check("filip", RIGHT, "203.0.113.9") is not None
    run(scenario())


def test_switching_somebody_off_ends_their_sessions(quick):
    async def scenario():
        await _person("filip")
        async with db.SessionLocal() as session:
            person = await accounts.by_name(session, "filip")
            version = person.version
            await accounts.amend(session, person, disabled=True)
            assert person.version > version
        assert await _check("filip", RIGHT, "192.168.1.20") is None
    run(scenario())


def test_a_recreated_name_cannot_accept_its_old_session(quick):
    async def scenario():
        async with db.SessionLocal() as session:
            first = await accounts.add(session, "filip", RIGHT)
            old_cookie = opus_auth.issue(settings.session_key, first.name, first.version)
            first_identity, first_version = first.identity, first.version
            await accounts.remove(session, first)
            second = await accounts.add(session, "filip", RIGHT)
            roster = await auth.roster(session)
            return old_cookie, first_identity, first_version, second, roster

    old_cookie, first_identity, first_version, second, roster = run(scenario())
    assert auth.whoami(roster, old_cookie) is None
    assert second.identity != first_identity
    assert second.version > first_version


def test_an_ipv6_caller_is_counted_by_its_network(quick):
    async def scenario():
        await _person("filip")
        for n in range(accounts.FAILS_PER_ADDRESS):
            await _wrong(f"guess{n % 3}", f"2001:db8:1:2::{n + 1:x}", 1)
        with pytest.raises(accounts.TooManyAttempts):
            await _check("filip", RIGHT, "2001:db8:1:2:ffff::9")
        assert await _check("filip", RIGHT, "2001:db8:1:3::1") is not None
    run(scenario())


def _held_hashing(monkeypatch):
    opened = threading.Event()
    hash_secret, secret_matches = accounts.hash_secret, accounts.secret_matches

    def held_hash(secret):
        opened.wait(10)
        return hash_secret(secret)

    def held_match(stored, secret):
        opened.wait(10)
        return secret_matches(stored, secret)

    monkeypatch.setattr(accounts, "hash_secret", held_hash)
    monkeypatch.setattr(accounts, "secret_matches", held_match)
    return opened


async def _outcome(name: str, address: str):
    try:
        return await _check(name, RIGHT, address)
    except (accounts.TooManyAttempts, accounts.Busy) as refused:
        return type(refused)


def test_a_burst_from_one_address_is_refused_before_it_waits(quick, monkeypatch):
    async def scenario():
        await _person("filip")
        opened = _held_hashing(monkeypatch)
        attempts = [asyncio.create_task(_outcome("filip", "203.0.113.9")) for _ in range(12)]
        async with asyncio.timeout(10):
            while accounts._queued < accounts.IN_FLIGHT_PER_ADDRESS:
                await asyncio.sleep(0.01)
        refused = [a.result() for a in attempts if a.done()]
        opened.set()
        rest = await asyncio.gather(*attempts)
        return refused, rest

    refused, rest = run(scenario())
    assert refused == [accounts.TooManyAttempts] * (12 - accounts.IN_FLIGHT_PER_ADDRESS)
    assert sum(1 for r in rest if r not in (accounts.TooManyAttempts, accounts.Busy)) \
        == accounts.IN_FLIGHT_PER_ADDRESS


def test_a_full_queue_says_so_and_empties(quick, monkeypatch):
    async def scenario():
        await _person("filip")
        opened = _held_hashing(monkeypatch)
        crowd = accounts.HASHING + accounts.WAITING
        attempts = [asyncio.create_task(_outcome("filip", f"198.51.100.{n}"))
                    for n in range(crowd + 5)]
        async with asyncio.timeout(10):
            while accounts._queued < crowd or sum(a.done() for a in attempts) < 5:
                await asyncio.sleep(0.01)
        refused = [a.result() for a in attempts if a.done()]
        opened.set()
        await asyncio.gather(*attempts)
        return refused, await _outcome("filip", "192.168.1.20")

    refused, later = run(scenario())
    assert refused == [accounts.Busy] * 5
    assert later is not None and later.name == "filip"
