"""Who may talk to the library.

Both halves were published on the open internet with no login at all — KLAPA and
KINO each answered `/api/settings` to anyone who asked. Merging them was the
moment to fix that once rather than twice.

One door, one middleware over every route. Two kinds of caller, because they are
not alike. A **person** arrives in a browser, proves themselves once and carries
a session cookie. A **consuming module** (Player, Downloads, the cameras) has
no browser and no session: it carries its own token on every call.

The roster is here, beside the catalogue — see opus.models.accounts for why. The
other two modules ask this one, and this one asks nobody: a module that has to
be running for anything to work is the right place for the answer everything
depends on.

Three standings, because a household is not two kinds of person. An **admin**
keeps the place. A **user** lives here: they read the whole shared library and
keep a vault of their own. A **guest** was handed the address for an evening and
gets the films, the series and the records — stated as an allowance rather than
a refusal, which is the only rule here written that way round.

What separates a user from an admin is one question, asked once: does this
request change anything. Reading is for everyone who lives here, because the
shared library is shared. Changing is the admin's, except your own account and
your own vault — and every one of these lists is a prefix match that fails
closed, so a route added tomorrow is the admin's until somebody says otherwise."""

import re

import opus_auth
from opus_auth import ADMIN, GUEST
from sqlalchemy import select

from opus.config import settings
from opus.models import User

# what answers before anyone has proved anything: liveness, and the exchange
# that proves it. Letting go of a session must not be able to fail because the
# session is already gone.
BOOTSTRAP_PATH = "/api/auth/bootstrap"
BOOTSTRAP_HEADER = "X-OPUS-Bootstrap-Key"
OPEN_PATHS = ("/api/ping", "/api/ready", "/api/auth/login", "/api/auth/session",
              "/api/auth/logout", "/api/auth/passkey/options", "/api/auth/passkey/login",
              BOOTSTRAP_PATH)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# A link handed to somebody outside the household: the key in the path is the
# whole of the proof, and what it opens is only read.
SHARED = ("/api/shared",)


# What a person who is not an admin may change. Everything else that changes
# anything is the admin's: the settings, the engines, the library's own upkeep,
# and every judgement about who is in a photograph. A user reads the family
# library, keeps their own vault, and looks after their own account — which is
# the whole of what an account in a household is for.
#
# Reading is not on this list because reading is not restricted: the shared
# library is shared. A path here is a prefix, and the rule fails closed — a
# route added tomorrow is the admin's until somebody says otherwise.
USERS_MAY = ("/api/photos/vault", "/api/photos/offer")


# Your own password and passkeys, which are the one change every standing may make.
OWN_ACCOUNT = ("/api/auth/password", "/api/auth/passkeys")


# The whole of what a guest may reach, which is the other way round from every
# other rule here: a guest is allowed a list rather than refused one. Somebody
# handed the address for an evening gets the films, the series and the records
# — and the photographs are not among them, because the family album is the one
# shelf that is nobody else's evening. Neither is the queue of what is being
# fetched, nor any vault, nor anything that describes the machine.
#
# Like the rule above it is a prefix and it fails closed. The words a plugin adds
# are on every screen, a guest's too.
GUESTS_MAY = ("/api/music", "/api/video", "/api/words")


# What the install is made of, as against what it holds. Reading the shelf is
# not restricted because the shelf is shared, but where the engines answer,
# which folders are scanned and what the thresholds are describe the machine —
# and that is no more a user's to read than it is theirs to change. The queue
# is the machine working, not the library; the boxes let in, the roster and
# the consumers' tokens are the door itself.
ADMIN_READS = ("/api/settings", "/api/downloads", "/api/auth/devices",
               "/api/auth/people", "/api/auth/token", "/api/storage", "/api/links",
               "/api/landing",
               "/api/music/downloads", "/api/music/library", "/api/video/library",
               "/api/photos/library", "/api/photos/contacts", "/api/photos/shares")


# Every consumer carries a token of its own, and each opens only what that
# consumer is built to ask: a token copied off the download box must not open
# the family album.
#
# The player reads the shelves and asks for what the household wants on them;
# it does not keep the install. What describes the machine stays closed to it
# except the three readings it is built on, and a change is refused unless it is
# one the player makes — a list rather than a refusal, so a route added tomorrow
# is not the token's either.
PLAYER_READS = ("/api/auth/people", "/api/storage", "/api/music/library/stats")


def _calls(*pairs: tuple[str, str]) -> tuple:
    return tuple((method, re.compile(pattern)) for method, pattern in pairs)


PLAYER_CHANGES = _calls(
    ("POST", r"/api/auth/verify"),
    ("POST", r"/api/auth/passkey/verify"),
    ("POST", r"/api/auth/devices/(ask|claim|verify)"),
    ("POST", r"/api/music/artists"),
    ("POST", r"/api/music/releases/\d+/download"),
    ("POST", r"/api/video/(movies|series)"),
    ("POST", r"/api/video/movies/\d+/search"),
    ("PATCH", r"/api/video/series/\d+/seasons/\d+"),
    ("POST", r"/api/video/series/\d+/seasons/\d+/search"),
    ("POST", r"/api/video/episodes/\d+/search"),
)

# Downloads keeps no roster: it asks who may sign in and whether a password is
# theirs, and nothing on the shelves is its business.
DOWNLOADS_CALLS = _calls(
    ("GET", r"/api/auth/people"),
    ("POST", r"/api/auth/verify"),
    ("POST", r"/api/auth/passkey/verify"),
)

# The cameras name the people they see from the same photographs: who they are,
# where their faces sit and the pictures those faces are cut from.
CAMERAS_CALLS = _calls(
    ("GET", r"/api/auth/people"),
    ("GET", r"/api/photos/people"),
    ("GET", r"/api/photos/people/\d+/faces"),
    ("GET", r"/api/photos/[0-9a-f]{40}/preview"),
    ("GET", r"/api/photos/faces/\d+/crop"),
)


def _among(calls: tuple, path: str, method: str) -> bool:
    return any(method == allowed and pattern.fullmatch(path) for allowed, pattern in calls)


def _player_may(path: str, method: str) -> bool:
    if method in SAFE_METHODS:
        return path in PLAYER_READS or not opus_auth.under(path, ADMIN_READS + USERS_MAY + OWN_ACCOUNT)
    return _among(PLAYER_CHANGES, path, method)


CONSUMERS = {
    "player": _player_may,
    "downloads": lambda path, method: _among(DOWNLOADS_CALLS, path, method),
    "cameras": lambda path, method: _among(CAMERAS_CALLS, path, method),
}


def token_key(consumer: str) -> str:
    return f"access_{consumer}_token"


def tokens(runtime) -> dict[str, str]:
    return {name: runtime.get(token_key(name)) for name in CONSUMERS}


def consumer(runtime, token: str | None) -> str | None:
    """Whose token this is, or nobody's."""
    return next((name for name, known in tokens(runtime).items()
                 if opus_auth.same_token(token, known)), None)


# Read by the guard on every request, and a thumbnail is a request. Held rather
# than asked because every write to it goes through opus.accounts, which drops
# this the moment it commits — so it is not a cache that can be stale, it is the
# same answer kept between two questions nobody changed anything in between.
_held: dict[str, tuple[int, str, bool]] | None = None


def forget_roster() -> None:
    global _held
    _held = None


async def roster(session) -> dict[str, tuple[int, str, bool]]:
    """Name to secret version, what standing they have here, and whether they
    have been switched off."""
    global _held
    if _held is not None:
        return _held
    rows = await session.execute(
        select(User.name, User.version, User.role, User.disabled))
    _held = {name: (version, role, disabled) for name, version, role, disabled in rows}
    return _held


def required(roster: dict) -> bool:
    """Whether somebody has completed the one-time account bootstrap."""
    return bool(roster)


def whoami(roster: dict, cookie: str | None) -> str | None:
    """The name this session belongs to, once the roster agrees it is current.

    A name the roster has never heard of is nobody: a person removed from it or
    switched off is out everywhere, immediately, cookie or no cookie."""
    said = opus_auth.session_user(settings.session_key, cookie)
    if said is None:
        return None
    name, version = said
    known, _, disabled = roster.get(name) or (None, "", True)
    return name if known == version and not disabled else None


def standing(roster: dict, cookie: str | None) -> str | None:
    """What this session may expect of the place, or None for a stranger.

    A name the roster no longer carries has no standing at all, which is how
    somebody removed is out everywhere within the same request."""
    name = whoami(roster, cookie)
    if name is None:
        return None
    return roster[name][1] or None


def allowed(runtime, roster: dict, path: str, cookie: str | None,
            token: str | None = None, method: str = "GET") -> bool:
    """Whether this caller may do this. Whether the request would alter
    something is the only question that separates a user from an admin —
    reading the shared library is what a user is here for."""
    if path in OPEN_PATHS or (opus_auth.under(path, SHARED) and method in SAFE_METHODS):
        return True
    # A new install has a narrow door for creating its first administrator;
    # exposing settings or library routes before that creates an unowned box.
    if not required(roster):
        return False
    holder = consumer(runtime, token)
    if holder is not None:
        return CONSUMERS[holder](path, method)
    changes = method not in SAFE_METHODS
    who = standing(roster, cookie)
    if who is None:
        return False
    # first, and for everybody: what describes the machine is the admin's
    # whoever is asking
    if opus_auth.under(path, ADMIN_READS):
        return who == ADMIN
    if opus_auth.under(path, OWN_ACCOUNT):
        return True
    if who == GUEST:
        # the one rule stated as an allowance rather than a refusal, and a guest
        # changes nothing anywhere: what they were given the address for is
        # watching and listening
        return opus_auth.under(path, GUESTS_MAY) and not changes
    if not changes or opus_auth.under(path, USERS_MAY):
        return True
    return who == ADMIN
