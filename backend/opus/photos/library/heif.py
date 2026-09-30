"""What a HEIF file says it contains, and whether it actually does.

A phone that runs out of space or loses the cable mid-copy can write a HEIC
whose header is complete and whose picture is not. The file declares its tiles
in `iinf` and says where each one lives in `iloc`; a tile can be declared and
have no entry at all, and then those pixels are simply not in the file.

No decoder tells us this. libheif 1.19 refuses the whole file with a message
about a missing `hvcC` box, which names a symptom three levels away from the
cause. 1.21 and 1.23 do something worse: they decode it, fill the absent tiles
with invented content and return success — 1.23 at least writes a warning to its
own stderr, 1.21 says nothing at all. Neither reaches us: libvips' `fail_on` does
not see those warnings and pillow-heif does not expose them. Measured, all four
combinations, before this file was written.

So the check is ours, it happens before any decoding, and it reads only the
header. It asks one question: does everything the primary picture is built from
have somewhere to be read from? A missing *auxiliary* item is a different
matter and deliberately not an error — one of the two files in this library is
missing five tiles of its HDR gain map while its primary grid is whole, and that
photograph is fine.

Anything this cannot parse is reported as fine. The parser is the new thing here
and libvips is the proven one; a file we cannot read the header of still gets
handed to the decoder, which has its own opinion and its own way of saying so.
"""

import struct


class Incomplete(Exception):
    """The file does not contain all of the picture it declares."""


# every item type that carries coded picture data. A grid is assembled from
# these; an item of any other type is metadata about the picture, not the
# picture, and its absence is not a hole in the photograph.
CODED = frozenset({"hvc1", "hev1", "av01", "avc1", "jpeg", "j2ki", "vvc1", "vvi1"})

# the header is small — 33 KB in the largest file in this library. Anything
# claiming a meta box larger than this is not something to read into memory on
# the strength of its own say-so.
MAX_META = 8 << 20

# ftyp plus the largest header this library holds (33 KB) with room over. The
# size is not a performance choice: measured over disjoint cold samples of 593
# files each, 4 KB and 128 KB run the same ~100 files a second, because the cost
# is opening the file and touching the disk at all, not how much comes back.
PREFIX = 64 << 10


def _boxes(d, off, end):
    """ISO-BMFF boxes in a byte range, as (type, payload_start, box_end)."""
    while off <= end - 8:
        size = struct.unpack_from(">I", d, off)[0]
        typ = d[off + 4:off + 8].decode("latin1", "replace")
        head = 8
        if size == 1:
            # the 64-bit form. A naive reader takes the literal 1 as the size
            # and concludes the file is truncated — which is exactly what a
            # first look at these two files concluded.
            size = struct.unpack_from(">Q", d, off + 8)[0]
            head = 16
        elif size == 0:
            size = end - off
        if size < head or off + size > end:
            return
        yield typ, off + head, off + size
        off += size


def _read_meta(fh):
    """The meta box, read without pulling the picture in behind it.

    One read rather than a seek per box, because `meta` follows `ftyp` within
    the first forty-odd bytes of every file this has met and a single prefix
    almost always holds the whole header. Simpler, not faster: the disk charges
    for being touched, and both shapes come out at about ten milliseconds a
    file — against four hundred to derive one, which is what makes checking
    every photograph affordable in the first place."""
    prefix = fh.read(PREFIX)
    off = 0
    while off <= len(prefix) - 8:
        size = struct.unpack_from(">I", prefix, off)[0]
        typ = prefix[off + 4:off + 8].decode("latin1", "replace")
        hdr = 8
        if size == 1:
            if off + 16 > len(prefix):
                return None
            size = struct.unpack_from(">Q", prefix, off + 8)[0]
            hdr = 16
        elif size == 0:
            fh.seek(0, 2)
            size = fh.tell() - off
        if size < hdr:
            return None
        if typ == "meta":
            want = size - hdr
            if want > MAX_META:
                return None
            body = prefix[off + hdr:off + size]
            if len(body) < want:                 # the rare header past the prefix
                fh.seek(off + hdr + len(body))
                body += fh.read(want - len(body))
            return body if len(body) == want else None
        off += size
    return None


def _items(d, s, e):
    """{item id: four-character type} from iinf."""
    version = d[s]
    off = s + 4
    if version == 0:
        off += 2
    else:
        off += 4
    out = {}
    for typ, a, b in _boxes(d, off, e):
        if typ != "infe":
            continue
        v = d[a]
        p = a + 4
        if v >= 3:
            iid = struct.unpack_from(">I", d, p)[0]
            p += 4
        else:
            iid = struct.unpack_from(">H", d, p)[0]
            p += 2
        p += 2  # protection index
        out[iid] = d[p:p + 4].decode("latin1", "replace")
    return out


def _located(d, s, e):
    """The set of item ids that iloc gives somewhere to read from."""
    version = d[s]
    off = s + 4
    packed = d[off]
    offset_size, length_size = packed >> 4, packed & 15
    off += 1
    packed = d[off]
    base_offset_size, index_size = packed >> 4, packed & 15
    off += 1
    if version < 2:
        count = struct.unpack_from(">H", d, off)[0]
        off += 2
    else:
        count = struct.unpack_from(">I", d, off)[0]
        off += 4
    out = set()
    for _ in range(count):
        if version < 2:
            iid = struct.unpack_from(">H", d, off)[0]
            off += 2
        else:
            iid = struct.unpack_from(">I", d, off)[0]
            off += 4
        if version in (1, 2):
            off += 2  # construction method
        off += 2 + base_offset_size
        extents = struct.unpack_from(">H", d, off)[0]
        off += 2
        for _ in range(extents):
            if version in (1, 2):
                off += index_size
            off += offset_size + length_size
        out.add(iid)
    return out


def _derived_from(d, s, e, parent):
    """The items `parent` is built out of, in the order it lays them out."""
    version = d[s]
    for typ, a, b in _boxes(d, s + 4, e):
        if typ != "dimg":
            continue
        if version == 0:
            frm = struct.unpack_from(">H", d, a)[0]
            n = struct.unpack_from(">H", d, a + 2)[0]
            to = [struct.unpack_from(">H", d, a + 4 + 2 * i)[0] for i in range(n)]
        else:
            frm = struct.unpack_from(">I", d, a)[0]
            n = struct.unpack_from(">H", d, a + 4)[0]
            to = [struct.unpack_from(">I", d, a + 6 + 4 * i)[0] for i in range(n)]
        if frm == parent:
            return to
    return []


def incomplete(path):
    """Why the primary picture cannot be whole, or None.

    None means either "the file is sound" or "this could not be read as HEIF",
    and the two are deliberately not distinguished: both end with the decoder
    being given the file and having its own say."""
    try:
        with open(path, "rb") as fh:
            meta = _read_meta(fh)
        if meta is None:
            return None
        children = {t: (a, b) for t, a, b in _boxes(meta, 4, len(meta))}
        if not {"iinf", "iloc", "pitm"} <= children.keys():
            return None

        s, _ = children["pitm"]
        version = meta[s]
        primary = (struct.unpack_from(">I", meta, s + 4)[0] if version
                   else struct.unpack_from(">H", meta, s + 4)[0])

        types = _items(meta, *children["iinf"])
        located = _located(meta, *children["iloc"])

        needed = [primary]
        if "iref" in children:
            tiles = _derived_from(meta, *children["iref"], primary)
            if tiles:
                needed = tiles

        gone = [i for i in needed if types.get(i) in CODED and i not in located]
        if not gone:
            return None
        if len(needed) == 1:
            return "the primary image has no data in the file"
        # said in the shape a person can act on: how much of the picture, and
        # roughly where, so they know whether to go looking for the original
        first = needed.index(gone[0]) + 1
        return ("%d of the %d tiles of the primary image are declared but have "
                "no data in the file, from tile %d — about %.0f%% of the frame "
                "is missing and no decoder can restore it"
                % (len(gone), len(needed), first, 100.0 * len(gone) / len(needed)))
    except (OSError, struct.error, IndexError, ValueError):
        return None
