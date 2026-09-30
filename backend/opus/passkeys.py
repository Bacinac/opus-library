"""Signing in with a passkey instead of the password.

The device keeps the private half and unlocks it with its owner's fingerprint,
face or PIN; the roster keeps the public half. The key belongs to the shared
domain, so one added here opens all three doors — but a door takes only what
was signed on its own address, so a sibling site on the same domain cannot
collect a signature and bring it here."""

import json
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from webauthn import (base64url_to_bytes, generate_authentication_options,
                      generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria,
                                      PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                      UserVerificationRequirement)

from opus.models import Passkey, User

# long enough for a person to find their phone, short enough that a question
# lying about is not worth collecting
LIFETIME = 120.0
# the question is asked before anybody has proved anything, so how many may be
# outstanding is bounded rather than trusted
OUTSTANDING = 256

_asked: OrderedDict[bytes, tuple[float, int | None]] = OrderedDict()


class Refused(Exception):
    """What came back proves nothing."""


def domain(configured: str) -> str | None:
    return configured.strip().lstrip(".").lower() or None


def _ask(user_id: int | None) -> bytes:
    now = time.monotonic()
    while _asked and (len(_asked) >= OUTSTANDING or next(iter(_asked.values()))[0] < now):
        _asked.popitem(last=False)
    challenge = secrets.token_bytes(32)
    _asked[challenge] = (now + LIFETIME, user_id)
    return challenge


def _answered(credential: dict, user_id: int | None) -> bytes:
    """The question this answer signed: once, fresh, and asked for this purpose."""
    try:
        client = json.loads(base64url_to_bytes(credential["response"]["clientDataJSON"]))
        challenge = base64url_to_bytes(client["challenge"])
    except (KeyError, TypeError, ValueError) as why:
        raise Refused("not a passkey's answer") from why
    asked = _asked.pop(challenge, None)
    if asked is None or asked[0] < time.monotonic() or asked[1] != user_id:
        raise Refused("the question was not ours, was answered already or has expired")
    return challenge


def _options(options) -> dict:
    return json.loads(options_to_json(options))


def sign_in_options(rp_id: str) -> dict:
    return _options(generate_authentication_options(
        rp_id=rp_id, challenge=_ask(None),
        user_verification=UserVerificationRequirement.REQUIRED))


async def signed_in(session, credential: dict, rp_id: str, origin: str) -> User:
    challenge = _answered(credential, None)
    try:
        key = (await session.execute(select(Passkey).where(
            Passkey.credential == base64url_to_bytes(credential["rawId"])))).scalar_one_or_none()
    except (KeyError, TypeError, ValueError) as why:
        raise Refused("not a passkey's answer") from why
    if key is None:
        raise Refused("no such passkey")
    try:
        verified = verify_authentication_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, credential_public_key=key.public_key,
            credential_current_sign_count=key.sign_count, require_user_verification=True)
    except WebAuthnException as why:
        raise Refused(str(why)) from why
    person = await session.get(User, key.user_id)
    if person is None or person.disabled:
        raise Refused("switched off")
    key.sign_count = verified.new_sign_count
    key.used_at = datetime.now(timezone.utc)
    await session.commit()
    return person


async def held(session, person: User) -> list[Passkey]:
    return list((await session.execute(select(Passkey).where(Passkey.user_id == person.id)
                                       .order_by(Passkey.created_at))).scalars())


async def adding_options(session, person: User, rp_id: str) -> dict:
    return _options(generate_registration_options(
        rp_id=rp_id, rp_name="OPUS", user_name=person.name,
        user_id=person.identity.encode(), user_display_name=person.display or person.name,
        challenge=_ask(person.id),
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=key.credential)
                             for key in await held(session, person)]))


async def add(session, person: User, credential: dict, rp_id: str, origin: str,
              name: str) -> Passkey:
    challenge = _answered(credential, person.id)
    try:
        verified = verify_registration_response(
            credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
            expected_origin=origin, require_user_verification=True)
    except WebAuthnException as why:
        raise Refused(str(why)) from why
    key = Passkey(user_id=person.id, credential=verified.credential_id,
                  public_key=verified.credential_public_key,
                  sign_count=verified.sign_count, name=name[:64])
    session.add(key)
    try:
        await session.commit()
    except IntegrityError as why:
        await session.rollback()
        raise Refused("this passkey is already here") from why
    return key


async def remove(session, person: User, key_id: int) -> bool:
    gone = await session.execute(delete(Passkey).where(
        Passkey.id == key_id, Passkey.user_id == person.id))
    await session.commit()
    return gone.rowcount > 0


def shown(key: Passkey) -> dict:
    return {"id": key.id, "name": key.name, "created_at": key.created_at,
            "used_at": key.used_at}


_BROWSERS = (("Edg/", "Edge"), ("SamsungBrowser/", "Samsung Internet"),
             ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))
# an iPhone says it is like Mac OS X and an Android phone that it is Linux, so
# the narrower word is looked for first
_SYSTEMS = (("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
            ("Windows", "Windows"), ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"),
            ("Linux", "Linux"))


def device_name(agent: str) -> str:
    """What the list calls a passkey, so the one to take back can be told apart:
    the browser and the system it was added from."""
    browser = next((name for mark, name in _BROWSERS if mark in agent), "")
    system = next((name for mark, name in _SYSTEMS if mark in agent), "")
    return " · ".join(part for part in (browser, system) if part)
