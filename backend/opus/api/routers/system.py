"""Endpoints that describe the installation rather than its contents: the
runtime settings the UI edits, the login and the consumers' tokens."""

import logging
import os
from pathlib import Path

import opus_auth
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opus import accounts, auth, devices, landing, passkeys
from opus.config import settings
from opus.db import get_session
from opus.models import Person, User
from opus.settings_store import (
    DIR_KEYS,
    SettingsValidationError,
    current_runtime,
    get_for_ui,
    store_credentials,
    update_settings,
)

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ping")
async def ping():
    """Liveness, and nothing else. A monitor should not need a credential, and
    must not learn anything by asking."""
    return {"ok": True}


@router.get("/ready")
async def ready(session: AsyncSession = Depends(get_session)):
    """Readiness: answer only after the database accepts a query.

    Unlike ``/ping``, this is deliberately allowed to fail while the process
    stays alive. A monitor can therefore distinguish a restart from a database
    outage without exposing installation state.
    """
    await session.execute(text("SELECT 1"))
    return {"ok": True}


@router.get("/storage")
async def storage():
    """How much room is left where the media sits.

    This module answers it because it is the one standing on the host the
    libraries are mounted on. Anything else that wants to show it — the player's
    settings, a dashboard — asks here rather than reaching for a filesystem it
    cannot see.

    Volumes, not folders: films and series share one pool and music sits beside
    the landing zone on another, so five configured directories are three disks,
    and listing them per folder counts the same terabytes as many times as they
    are written to.

    Grouped by size rather than by device id, because a device id does not mean
    what it appears to: mergerfs mounted at two points is two device ids and one
    pool of disks, and grouping by st_dev reported the same seventeen terabytes
    twice. Two mounts of the same capacity with the same room left are the same
    storage — free space is rounded to the gibibyte so a write between the two
    measurements does not split them apart."""
    config = await current_runtime()
    volumes: dict[tuple[int, int], dict] = {}
    for key in DIR_KEYS:
        path = config.get(key)
        if not path or not Path(path).is_dir():
            continue
        info = os.statvfs(path)
        if not info.f_blocks:
            continue
        total = info.f_blocks * info.f_frsize
        free = info.f_bavail * info.f_frsize
        volumes.setdefault((total, round(free / 1024 ** 3)), {
            "holds": [],
            "path": path,
            "total": total,
            "used": (info.f_blocks - info.f_bfree) * info.f_frsize,
            "free": free,
        })["holds"].append(key.removesuffix("_dir").removeprefix("opus_"))
    return {"volumes": sorted(volumes.values(), key=lambda v: -v["total"])}


@router.get("/landing")
async def landing_preview(session: AsyncSession = Depends(get_session)):
    """Show the next landing-zone sweep without removing anything.

    It is an administrator-only installation reading (see ``ADMIN_READS``),
    because transfer names and the path layout are operational information.
    """
    where = await landing.root()
    if where is None:
        raise HTTPException(503, "landing zone is unavailable")
    config = await current_runtime()
    keep = config.float("landing_keep_days")
    active = await landing.active_folders(session, where)
    return await landing.preview(where, keep, active)


# which vocabulary this install speaks. A type is present when its library
# directory is really there — the compose binds a tree only if the .env names
# one — so an install with only music is a music app, with no film word
# anywhere in it, and nobody had to switch anything off.
_TYPE_DIRS = (("music", "music_dir"), ("movies", "movies_dir"),
              ("series", "tv_dir"), ("video", "video_dir"),
              ("photos", "photos_dir"))


@router.get("/types")
async def types():
    config = await current_runtime()
    return [t for t, key in _TYPE_DIRS if Path(config.get(key)).is_dir()]


class Login(BaseModel):
    username: str
    password: str


class NewPassword(BaseModel):
    current: str
    password: str


class PasskeyAnswer(BaseModel):
    credential: dict


class RelayedPasskey(BaseModel):
    credential: dict
    origin: str


class Current(BaseModel):
    current: str


class Asking(BaseModel):
    module: str = ""


class Held(BaseModel):
    token: str


class Claim(BaseModel):
    id: int
    claim: str


class LettingIn(BaseModel):
    code: str
    name: str = ""


class Naming(BaseModel):
    name: str


class NewPerson(BaseModel):
    name: str
    password: str
    display: str = ""
    role: str = opus_auth.USER


class Amendment(BaseModel):
    display: str | None = None
    password: str | None = None
    role: str | None = None
    disabled: bool | None = None
    person_id: int | None = None
    unlink: bool = False


async def _me(request: Request, session: AsyncSession):
    name = auth.whoami(await auth.roster(session),
                       request.cookies.get(opus_auth.SESSION_COOKIE))
    return await accounts.by_name(session, name) if name else None


def _service(request: Request, config):
    """Only a module, and only with the household's own token.

    For the boxes, which have no credential yet — that is the whole reason they
    are here — so what vouches for the request is the module the box talks to.
    A person's session opens nothing here on purpose: signing in somewhere else
    must not be a way to mint a credential for a television. And for checking a
    password on a person's behalf, which is a module's question and not a
    stranger's."""
    if auth.consumer(config, request.headers.get(opus_auth.TOKEN_HEADER)) is None:
        raise HTTPException(403, "only a module may ask this")


async def _admin(request: Request, session: AsyncSession):
    person = await _me(request, session)
    if person is None or person.role != opus_auth.ADMIN:
        # a service token opens routes, not accounts: a module acts for the
        # household and has no business making people
        raise HTTPException(403, "only an admin may do this")
    return person


def _sign_in(response: Response, request: Request, person):
    opus_auth.set_cookie(response, request, settings.cookie_domain, opus_auth.SESSION_COOKIE,
                         opus_auth.issue(settings.session_key, person.name, person.version),
                         opus_auth.SESSION_MAX_AGE)


async def _checked(session, request: Request, name: str, secret: str):
    try:
        relayed = auth.consumer(await current_runtime(),
                                request.headers.get(opus_auth.TOKEN_HEADER)) is not None
        return await accounts.check(session, name, secret,
                                    opus_auth.client_address(request, relayed=relayed))
    except (accounts.TooManyAttempts, accounts.Busy) as refused:
        raise HTTPException(429, str(refused),
                            headers={"Retry-After": str(refused.retry_after)})


def _bootstrap(request: Request) -> None:
    if not opus_auth.same_token(request.headers.get(auth.BOOTSTRAP_HEADER),
                                settings.bootstrap_key):
        raise HTTPException(401, "the bootstrap key is required")


@router.get("/auth/session")
async def auth_session(request: Request, session: AsyncSession = Depends(get_session)):
    roster = await auth.roster(session)
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    who = auth.whoami(roster, cookie)
    return {
        # A first-run install is closed until its bootstrap credential creates
        # an admin; the UI must not mistake its absence for public access.
        "required": True,
        "authenticated": who is not None,
        "bootstrap": not auth.required(roster),
        "username": who or "",
        # what the screen needs to stop offering what the server would refuse:
        # an admin maintains the library, a user reads it and keeps their own
        # vault, a guest is here for the films and the records
        "role": auth.standing(roster, cookie) or "",
        "passkey": opus_auth.passkey_door(settings.cookie_domain, request) is not None,
    }


@router.post("/auth/verify")
async def auth_verify(body: Login, request: Request,
                      session: AsyncSession = Depends(get_session)):
    """Is this the credential — asked by a module that keeps none of its own.

    It says who, and never sets a cookie: the asking module issues its own
    session and needs the version to sign it with. Only a module asks, so the
    address it names for the person is one worth counting failures against."""
    config = await current_runtime()
    _service(request, config)
    if not auth.required(await auth.roster(session)):
        return {"ok": False, "bootstrap": True}
    person = await _checked(session, request, body.username, body.password)
    return {"ok": True, **accounts.shown(person)} if person else {"ok": False}


@router.post("/auth/login")
async def auth_login(body: Login, request: Request, response: Response,
                     session: AsyncSession = Depends(get_session)):
    if not auth.required(await auth.roster(session)):
        raise HTTPException(409, "create the first admin with the bootstrap key")
    person = await _checked(session, request, body.username, body.password)
    if person is None:
        # one message for a wrong name and a wrong password: which of the two
        # was right is not the asker's business
        raise HTTPException(401, "wrong username or password")
    _sign_in(response, request, person)
    return {"ok": True, **accounts.shown(person)}


@router.post("/auth/bootstrap")
async def auth_bootstrap(body: NewPerson, request: Request,
                         session: AsyncSession = Depends(get_session)):
    """Create the sole first administrator, guarded by an environment secret."""
    if auth.required(await auth.roster(session)):
        raise HTTPException(409, "an administrator already exists")
    if not settings.bootstrap_key:
        raise HTTPException(503, "OPUS_BOOTSTRAP_KEY must be configured before first setup")
    _bootstrap(request)
    name = accounts.tidy_name(body.name)
    if not name:
        raise HTTPException(400, "a person needs a name")
    if accounts.too_short(body.password):
        raise HTTPException(400, f"a password needs {accounts.SHORTEST} characters")
    person = await accounts.add(session, name, body.password, body.display, opus_auth.ADMIN)
    return accounts.shown(person)


@router.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    opus_auth.delete_cookie(response, request, settings.cookie_domain, opus_auth.SESSION_COOKIE)
    return {"ok": True}


@router.post("/auth/password")
async def auth_password(body: NewPassword, request: Request, response: Response,
                        session: AsyncSession = Depends(get_session)):
    """The current one is asked for again even though this call is already
    authenticated: a session left open on a borrowed screen should not be able
    to take the account with it."""
    person = await _me(request, session)
    if person is None or await _checked(session, request, person.name, body.current) is None:
        return Response(status_code=401)
    if accounts.too_short(body.password):
        raise HTTPException(400, f"a password needs {accounts.SHORTEST} characters")
    await accounts.amend(session, person, secret=body.password)
    # the version just rose, so every session issued under the old secret ended
    # — including this caller's, who has to keep theirs
    _sign_in(response, request, person)
    return {"changed": True, "version": person.version}


def _door(request: Request) -> tuple[str, str]:
    door = opus_auth.passkey_door(settings.cookie_domain, request)
    if door is None:
        raise HTTPException(404, "this door takes no passkey")
    return door


def _shared_domain() -> str:
    rp_id = passkeys.domain(settings.cookie_domain)
    if rp_id is None:
        raise HTTPException(404, "this install has no shared domain for a passkey")
    return rp_id


async def _passkey(session, credential: dict, rp_id: str, origin: str):
    try:
        return await passkeys.signed_in(session, credential, rp_id, origin)
    except passkeys.Refused as why:
        log.info("passkey refused at %s: %s", origin, why)
        return None


@router.post("/auth/passkey/options")
async def auth_passkey_options(request: Request):
    """The question a passkey answers. A module asking on a browser's behalf
    reaches this over the internal network, so the configured domain stands for
    it; a browser here gets one only on this door."""
    if auth.consumer(await current_runtime(), request.headers.get(opus_auth.TOKEN_HEADER)):
        return passkeys.sign_in_options(_shared_domain())
    return passkeys.sign_in_options(_door(request)[0])


@router.post("/auth/passkey/login")
async def auth_passkey_login(body: PasskeyAnswer, request: Request, response: Response,
                             session: AsyncSession = Depends(get_session)):
    person = await _passkey(session, body.credential, *_door(request))
    if person is None:
        raise HTTPException(401, "the passkey was not accepted")
    _sign_in(response, request, person)
    return {"ok": True, **accounts.shown(person)}


@router.post("/auth/passkey/verify")
async def auth_passkey_verify(body: RelayedPasskey, request: Request,
                              session: AsyncSession = Depends(get_session)):
    """Whose passkey signed this — asked by a module, which names its own door
    as the origin the signature has to carry."""
    _service(request, await current_runtime())
    person = await _passkey(session, body.credential, _shared_domain(), body.origin)
    return {"ok": True, **accounts.shown(person)} if person else {"ok": False}


@router.get("/auth/passkeys")
async def auth_passkeys(request: Request, session: AsyncSession = Depends(get_session)):
    person = await _me(request, session)
    if person is None:
        return Response(status_code=401)
    return {"passkeys": [passkeys.shown(key) for key in await passkeys.held(session, person)]}


@router.post("/auth/passkeys/options")
async def auth_passkey_adding(body: Current, request: Request,
                              session: AsyncSession = Depends(get_session)):
    """Adding a way in asks for the password first, for the same reason changing
    it does: a session left open on a borrowed screen must not be able to leave
    a key of its own behind."""
    person = await _me(request, session)
    if person is None or await _checked(session, request, person.name, body.current) is None:
        return Response(status_code=401)
    return await passkeys.adding_options(session, person, _door(request)[0])


@router.post("/auth/passkeys")
async def auth_passkey_add(body: PasskeyAnswer, request: Request,
                           session: AsyncSession = Depends(get_session)):
    person = await _me(request, session)
    if person is None:
        return Response(status_code=401)
    try:
        key = await passkeys.add(session, person, body.credential, *_door(request),
                                 passkeys.device_name(request.headers.get("user-agent", "")))
    except passkeys.Refused as why:
        log.info("passkey not added for %s: %s", person.name, why)
        raise HTTPException(400, "the passkey was not accepted")
    return passkeys.shown(key)


@router.delete("/auth/passkeys/{key_id}")
async def auth_passkey_remove(key_id: int, request: Request,
                              session: AsyncSession = Depends(get_session)):
    person = await _me(request, session)
    if person is None:
        return Response(status_code=401)
    if not await passkeys.remove(session, person, key_id):
        raise HTTPException(404, "no such passkey")
    return {"removed": True}


@router.get("/auth/people")
async def auth_people(session: AsyncSession = Depends(get_session)):
    """Everyone who may sign in. Asked by the other modules with the service
    token, which hold no roster of their own and need one to guard with, and
    by an admin keeping the list."""
    return {"people": [accounts.shown(p) for p in await accounts.everyone(session)]}


@router.post("/auth/people")
async def auth_add_person(body: NewPerson, request: Request,
                          session: AsyncSession = Depends(get_session)):
    await _admin(request, session)
    name = accounts.tidy_name(body.name)
    if not name:
        raise HTTPException(400, "a person needs a name")
    if accounts.too_short(body.password):
        raise HTTPException(400, f"a password needs {accounts.SHORTEST} characters")
    if await accounts.by_name(session, name):
        raise HTTPException(409, "that name is taken")
    person = await accounts.add(session, name, body.password, body.display, body.role)
    return accounts.shown(person)


@router.patch("/auth/people/{name}")
async def auth_amend_person(name: str, body: Amendment, request: Request,
                            session: AsyncSession = Depends(get_session)):
    me = await _admin(request, session)
    person = await accounts.by_name(session, name)
    if person is None:
        raise HTTPException(404, "no such person")
    if body.password is not None and accounts.too_short(body.password):
        raise HTTPException(400, f"a password needs {accounts.SHORTEST} characters")
    stepping_down = person.role == opus_auth.ADMIN and (
        (body.role is not None and accounts.standing(body.role) != opus_auth.ADMIN)
        or body.disabled is True)
    if stepping_down and await accounts.admins(session) < 2:
        # an install with no admin has nobody who can make one
        raise HTTPException(409, "the last admin cannot step down")
    if body.person_id is not None and not body.unlink:
        if await session.get(Person, body.person_id) is None:
            raise HTTPException(404, "no such face in the photographs")
        holder = await session.scalar(
            select(User.name).where(User.person_id == body.person_id, User.id != person.id))
        if holder is not None:
            raise HTTPException(409, f"that face is already {holder}'s account")
    await accounts.amend(session, person, display=body.display,
                         secret=body.password, role=body.role,
                         disabled=body.disabled, person_id=body.person_id,
                         unlink=body.unlink)
    return accounts.shown(person) | {"was_me": person.id == me.id}


@router.delete("/auth/people/{name}")
async def auth_remove_person(name: str, request: Request,
                             session: AsyncSession = Depends(get_session)):
    me = await _admin(request, session)
    person = await accounts.by_name(session, name)
    if person is None:
        raise HTTPException(404, "no such person")
    if person.id == me.id:
        raise HTTPException(409, "removing yourself would end the session doing it")
    if person.role == opus_auth.ADMIN and await accounts.admins(session) < 2:
        raise HTTPException(409, "the last admin cannot be removed")
    await accounts.remove(session, person)
    return {"removed": person.name}


@router.post("/auth/devices/ask")
async def devices_ask(body: Asking, request: Request,
                      session: AsyncSession = Depends(get_session)):
    """A box asking to be let in, relayed by the module it talks to.

    Only a module may ask — the service token and nothing else. A box has no
    credential yet, which is the whole point of it being here, so the thing that
    vouches for the request is the module that already carries the household's
    own. What comes back is worth nothing until somebody says yes to it, and the
    claim in it is what the module keeps for the box and never shows."""
    _service(request, await current_runtime())
    device, claim = await devices.ask(session, body.module)
    return {"code": device.code, "id": device.id, "claim": claim, "minutes": int(
        devices.GOOD_FOR.total_seconds() // 60)}


@router.post("/auth/devices/claim")
async def devices_claim(body: Claim, request: Request,
                        session: AsyncSession = Depends(get_session)):
    """Has anybody said yes yet — asked over and over for a box watching its own
    code on the screen, by the module holding that box's claim.

    The token comes back once and only once: it is minted at the moment the
    claim collects it, and this row cannot produce it again afterwards."""
    _service(request, await current_runtime())
    said = await devices.collect(session, body.id, body.claim)
    if said is None:
        raise HTTPException(404, "no such request")
    return said


@router.get("/auth/devices")
async def devices_list(request: Request, session: AsyncSession = Depends(get_session)):
    await _admin(request, session)
    return {"devices": [devices.shown(d) for d in await devices.everyone(session)]}


@router.post("/auth/devices")
async def devices_let_in(body: LettingIn, request: Request,
                         session: AsyncSession = Depends(get_session)):
    """Say yes to a code somebody is reading off a screen."""
    me = await _admin(request, session)
    device = await devices.waiting(session, body.code)
    if device is None:
        # one message for a code that never existed, one that was already used
        # and one that ran out: which of the three it was is not worth telling
        # somebody guessing
        raise HTTPException(404, "that code is not waiting for anything")
    await devices.let_in(session, device, body.name, me.name)
    return devices.shown(device)


@router.patch("/auth/devices/{device_id}")
async def devices_rename(device_id: int, body: Naming, request: Request,
                         session: AsyncSession = Depends(get_session)):
    await _admin(request, session)
    device = await devices.claimed(session, device_id)
    if device is None or device.let_in_at is None:
        raise HTTPException(404, "no such device")
    if not body.name.strip():
        raise HTTPException(422, "a box is known by its name")
    await devices.rename(session, device, body.name)
    return devices.shown(device)


@router.delete("/auth/devices/{device_id}")
async def devices_take_back(device_id: int, request: Request,
                            session: AsyncSession = Depends(get_session)):
    await _admin(request, session)
    device = await devices.claimed(session, device_id)
    if device is None:
        raise HTTPException(404, "no such device")
    await devices.take_back(session, device)
    return {"gone": device.name}


@router.post("/auth/devices/verify")
async def devices_verify(body: Held, request: Request,
                         session: AsyncSession = Depends(get_session)):
    """Is this box still let in — asked by the module holding the box's token.

    Says nothing about who is watching. A device is an appliance and this answer
    is only ever "yes, and it is called the living room", which is exactly as
    much as a screen in a shared room should be able to establish about itself."""
    _service(request, await current_runtime())
    device = await devices.holding(session, body.token)
    if device is None:
        return {"ok": False}
    await devices.seen(session, device)
    return {"ok": True, "id": device.id, "name": device.name}


@router.get("/auth/token")
async def auth_tokens():
    """The token each consuming module calls with, for an admin to carry to that
    module by hand. Empty until one is made."""
    held = auth.tokens(await current_runtime())
    return {"tokens": [{"consumer": name, "token": token} for name, token in held.items()]}


class TokenFor(BaseModel):
    consumer: str


@router.post("/auth/token")
async def auth_new_token(body: TokenFor, request: Request,
                         session: AsyncSession = Depends(get_session)):
    """A new token for one consumer, which ends its old one: that module is
    turned away until it is given this one, and the others are untouched."""
    await _admin(request, session)
    if body.consumer not in auth.CONSUMERS:
        raise HTTPException(404, "no such consumer")
    value = opus_auth.new_token()
    await store_credentials({auth.token_key(body.consumer): value})
    return {"consumer": body.consumer, "token": value}


@router.get("/settings")
async def read_settings():
    return await get_for_ui()


@router.put("/settings")
async def write_settings(updates: dict[str, str]):
    try:
        await update_settings(updates)
    except SettingsValidationError as exc:
        raise HTTPException(400, {"key": exc.key, "code": exc.code})
    return await get_for_ui()
