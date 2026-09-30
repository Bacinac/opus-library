"""Accounts linked through a device-code approval.

A service that hands a token to whoever approves a link on its own site has no
credential to type, so its account is linked here instead: ask, approve in
another tab, and the answer arrives on a later look. The linked account is the
install's, not a record's. Which services link this way is the plugins' to say
(opus.plugins)."""

from typing import Protocol

from fastapi import APIRouter, HTTPException
from opus_core import plugins

from opus.plugins import LINKS

router = APIRouter(prefix="/links")


class Link(Protocol):
    async def login_status(self) -> dict:
        """pending, url and error of the approval under way, and the linked
        accounts as id, label and created_at."""

    async def start_login(self) -> dict:
        """Begin an approval and answer the url it waits at."""

    async def delete_account(self, account_id: int) -> bool: ...


def _link(name: str) -> Link:
    paths = dict(LINKS)
    if name not in paths:
        raise HTTPException(404, f"nothing links an account named {name}")
    return plugins.resolve(paths[name])


@router.get("")
async def links():
    return [{"name": name, **await _link(name).login_status()} for name, _ in LINKS]


@router.post("/{name}", status_code=202)
async def start_login(name: str):
    link = _link(name)
    try:
        return await link.start_login()
    except Exception as exc:
        raise HTTPException(502, f"{name} login could not start: {exc}")


@router.delete("/{name}/{account_id}", status_code=204)
async def unlink(name: str, account_id: int):
    if not await _link(name).delete_account(account_id):
        raise HTTPException(404, "account not found")
