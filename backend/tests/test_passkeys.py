import base64
import hashlib
import json
import secrets

import cbor2
import opus_auth
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import insert

from conftest import library, run, signed_in
from opus import auth, db, passkeys, settings_store
from opus.config import settings
from opus.models import Setting

DOMAIN = "example.com"
LIBRARY = "library.example.com"
PLAYER = "https://opus.example.com"
SIBLING = "https://other.example.com"
PLAYER_TOKEN = "player-token-0123456789"
PHONE = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36")
AT_LIBRARY = {"x-forwarded-host": LIBRARY, "user-agent": PHONE}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Device:
    """A phone's passkey store, reduced to the one key it makes and signs with."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential = secrets.token_bytes(16)
        self.count = 0
        self.user = b""

    def _client(self, kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin,
                           "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str) -> dict:
        self.user = _unb64(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"),
                            -3: numbers.y.to_bytes(32, "big")})
        data = (hashlib.sha256(options["rp"]["id"].encode()).digest() + bytes([0x45])
                + self.count.to_bytes(4, "big") + bytes(16)
                + len(self.credential).to_bytes(2, "big") + self.credential + cose)
        return {"id": _b64(self.credential), "rawId": _b64(self.credential), "type": "public-key",
                "response": {
                    "clientDataJSON": _b64(self._client("webauthn.create", options["challenge"], origin)),
                    "attestationObject": _b64(cbor2.dumps(
                        {"fmt": "none", "attStmt": {}, "authData": data}))},
                "clientExtensionResults": {}}

    def sign(self, options: dict, origin: str) -> dict:
        self.count += 1
        data = (hashlib.sha256(options["rpId"].encode()).digest() + bytes([0x05])
                + self.count.to_bytes(4, "big"))
        client = self._client("webauthn.get", options["challenge"], origin)
        signature = self.key.sign(data + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": _b64(self.credential), "rawId": _b64(self.credential), "type": "public-key",
                "response": {"clientDataJSON": _b64(client), "authenticatorData": _b64(data),
                             "signature": _b64(signature), "userHandle": _b64(self.user)},
                "clientExtensionResults": {}}


@pytest.fixture
def shared(quick, monkeypatch):
    monkeypatch.setattr(settings, "cookie_domain", DOMAIN)
    passkeys._asked.clear()


async def _added(device: Device, cookie: str):
    async with library(cookie) as client:
        options = (await client.post("/api/auth/passkeys/options",
                                     json={"current": "a-long-enough-secret"},
                                     headers=AT_LIBRARY)).json()
        return await client.post("/api/auth/passkeys", headers=AT_LIBRARY,
                                 json={"credential": device.create(options, f"https://{LIBRARY}")})


async def _sign_in(device: Device, origin: str = f"https://{LIBRARY}"):
    async with library() as client:
        options = (await client.post("/api/auth/passkey/options", headers=AT_LIBRARY)).json()
        return await client.post("/api/auth/passkey/login", headers=AT_LIBRARY,
                                 json={"credential": device.sign(options, origin)})


def test_a_passkey_added_once_signs_in_and_is_listed(shared):
    device = Device()

    async def scenario():
        cookie = await signed_in("filip", role="user")
        added = await _added(device, cookie)
        entered = await _sign_in(device)
        async with library(cookie) as client:
            listed = (await client.get("/api/auth/passkeys")).json()["passkeys"]
        return added, entered, listed

    added, entered, listed = run(scenario())
    assert added.status_code == 200
    assert added.json()["name"] == "Chrome · Android"
    assert entered.status_code == 200
    assert entered.json()["name"] == "filip"
    assert entered.headers["set-cookie"].startswith(f"{opus_auth.SESSION_COOKIE}=")
    assert [key["name"] for key in listed] == ["Chrome · Android"]
    assert listed[0]["used_at"] is not None


def test_adding_asks_for_the_password_first(shared):
    async def scenario():
        cookie = await signed_in("filip", role="guest")
        async with library(cookie) as client:
            return await client.post("/api/auth/passkeys/options", json={"current": "not-it"},
                                     headers=AT_LIBRARY)

    assert run(scenario()).status_code == 401


def test_a_signature_is_taken_once_and_only_on_its_own_door(shared):
    device = Device()

    async def scenario():
        await _added(device, await signed_in("filip", role="user"))
        async with library() as client:
            options = (await client.post("/api/auth/passkey/options", headers=AT_LIBRARY)).json()
            answer = {"credential": device.sign(options, f"https://{LIBRARY}")}
            first = await client.post("/api/auth/passkey/login", headers=AT_LIBRARY, json=answer)
            again = await client.post("/api/auth/passkey/login", headers=AT_LIBRARY, json=answer)
        sibling = await _sign_in(device, SIBLING)
        return first, again, sibling

    first, again, sibling = run(scenario())
    assert first.status_code == 200
    assert (again.status_code, sibling.status_code) == (401, 401)
    assert "set-cookie" not in sibling.headers


def test_a_module_s_door_is_the_origin_it_names(shared):
    device = Device()

    async def scenario():
        await _added(device, await signed_in("filip", role="user"))
        async with db.SessionLocal() as session:
            await session.execute(insert(Setting).values(key=auth.token_key("player"),
                                                         value=PLAYER_TOKEN))
            await session.commit()
        settings_store.forget_runtime()
        said = []
        async with library() as client:
            client.headers[opus_auth.TOKEN_HEADER] = PLAYER_TOKEN
            for signed_at in (PLAYER, SIBLING):
                options = (await client.post("/api/auth/passkey/options")).json()
                said.append((await client.post("/api/auth/passkey/verify", json={
                    "credential": device.sign(options, signed_at), "origin": PLAYER})).json())
        return options, said

    options, (player, sibling) = run(scenario())
    assert options["rpId"] == DOMAIN
    assert options["userVerification"] == "required"
    assert player["ok"] and player["name"] == "filip"
    assert sibling == {"ok": False}


def test_a_passkey_is_taken_back_only_by_its_owner(shared):
    device = Device()

    async def scenario():
        filip, kata = await signed_in("filip", role="user"), await signed_in("kata", role="user")
        key = (await _added(device, filip)).json()["id"]
        async with library(kata) as client:
            stranger = await client.delete(f"/api/auth/passkeys/{key}")
        async with library(filip) as client:
            owner = await client.delete(f"/api/auth/passkeys/{key}")
        return stranger, owner, await _sign_in(device)

    stranger, owner, entered = run(scenario())
    assert stranger.status_code == 404
    assert owner.status_code == 200
    assert entered.status_code == 401


def test_the_door_says_whether_it_takes_a_passkey(shared):
    lan = {"x-forwarded-host": "192.168.1.103:5280"}

    async def scenario():
        async with library() as client:
            return ((await client.get("/api/auth/session", headers=AT_LIBRARY)).json()["passkey"],
                    (await client.get("/api/auth/session", headers=lan)).json()["passkey"],
                    (await client.post("/api/auth/passkey/options", headers=lan)).status_code)

    assert run(scenario()) == (True, False, 404)


@pytest.mark.parametrize(("agent", "name"), [
    (PHONE, "Chrome · Android"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1", "Safari · iPhone"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0", "Edge · Windows"),
    ("", ""),
])
def test_a_passkey_is_named_after_where_it_was_added(agent, name):
    assert passkeys.device_name(agent) == name
