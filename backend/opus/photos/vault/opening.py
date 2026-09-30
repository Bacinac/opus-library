"""Opening exactly one picture, because its owner asked us to.

This is the only place in the install that decrypts anything out of a vault, and
it does so on a key handed over for the occasion. The distinction is the whole
design: the server holds no vault key and cannot derive one, so it can read a
file only when the person who owns it says which file and gives the key to that
file alone.

Which costs nothing, because the picture is on its way into the family library —
it is about to stop being private by the owner's own decision. What does not
move is everything else: each file was sealed under its own random content key,
so the one handed over opens one picture and no other.

The alternative was to have the device decrypt and send the bytes up again. That
works and was what this did first, but it means a second journey for every
offered file — nothing for a photograph, an evening on a phone connection for a
video of two gigabytes."""

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# what AES-GCM appends to every piece it seals
TAG = 16


class WrongKey(Exception):
    """The key does not open this file. Said plainly rather than as a partly
    written file in the tree."""


class NotWhole(Exception):
    """The file on disk is not the length it was announced at. Every piece
    carries its own tag and its own position, so pieces missing from the end
    leave the rest opening cleanly: only the length tells."""


def _nonce(base: bytes, n: int) -> bytes:
    return base + n.to_bytes(4, "big")


def open_into(sealed: Path, into: Path, key: bytes, base: bytes,
              chunk: int, length: int) -> tuple[bytes, int]:
    """Decrypt one vault file into a path, in pieces, and say what it is.

    Piece by piece and straight to disk because the thing being opened may be a
    video: holding it whole would put a phone's memory problem on the server.
    The digest is taken on the way past, so the plaintext is never read twice.

    A tag that does not verify stops everything and removes what was written.
    Half a file in the photo tree would be adopted by the next pass as a
    photograph, which it is not."""
    if len(base) != 8 or len(key) != 32:
        raise WrongKey("that is not a key and a nonce for this vault")
    if sealed.stat().st_size != length:
        raise NotWhole("the file in the vault is not whole")
    cipher = AESGCM(key)
    digest = hashlib.sha1()
    written = 0
    try:
        with sealed.open("rb") as source, into.open("wb") as target:
            n = 0
            while piece := source.read(chunk + TAG):
                plain = cipher.decrypt(_nonce(base, n), piece, None)
                digest.update(plain)
                target.write(plain)
                written += len(plain)
                n += 1
    except InvalidTag:
        into.unlink(missing_ok=True)
        raise WrongKey("the key does not open this file")
    except Exception:
        into.unlink(missing_ok=True)
        raise
    if not written:
        into.unlink(missing_ok=True)
        raise WrongKey("there was nothing to open")
    return digest.digest(), written
