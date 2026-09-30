"""Audio quality as a measured value rather than a format family.

A usenet post states its quality in the title and nowhere else, and it does so
in a dozen scene dialects: '24BIT-96KHZ-WEB-FLAC', '24-96-WEB-FLAC',
'[16BIT]-[WEBFLAC]', '.flac.48-24', 'CD-FLAC', '(320kb)'. What is parsed here
is only what the title actually says: a rip labelled 'CD-FLAC' is FLAC of an
unstated resolution, not a 16/44.1 release, because the ceiling below excludes
what stands above it and unknown must never be thrown away for a number nobody
wrote down.

Pure: no network, no database, no ORM — a title or a file extension in, a
Quality out."""

import re
from dataclasses import dataclass, fields

LOSSLESS_CODECS = frozenset({"flac", "alac", "wav", "aiff", "ape", "wv", "dsd"})

# scene tags write kHz as a bare integer; the Red Book family is 44.1/88.2/176.4
# and comparing '44' against a 44.1 ceiling has to come out equal
_RATES = {44: 44.1, 48: 48.0, 64: 64.0, 88: 88.2, 96: 96.0,
          176: 176.4, 192: 192.0, 352: 352.8, 384: 384.0}
_DEPTHS = frozenset({8, 16, 20, 24, 32})

# a number that opens a token: '.320kb' counts, the '.1' of '44.1' does not
_NUM = r"(?<!\d)(?<!\d\.)"
# a codec word glued to letters is something else ('FLACX' is a group name),
# glued to digits it is not ('24-192flac'). The ones that are ordinary English
# need a scene separator on both sides, or every 'Magnum Opus' becomes Opus.
_SEP = r"[-._\[\](){}+/,|]"
_CODEC_PATTERNS = (
    ("flac", r"(?<![a-z])(?:webflac|flac)(?![a-z])"),
    ("alac", r"(?<![a-z])alac(?![a-z])"),
    ("aiff", r"(?<![a-z])aiff?(?![a-z])"),
    ("dsd", r"(?<![a-z])(?:dsd\d*|dsf|dff)(?![a-z])"),
    ("wv", rf"(?:(?<={_SEP})|^)(?:wv|wavpack)(?={_SEP}|$)"),
    ("wav", rf"(?:(?<={_SEP})|^)wav(?={_SEP}|$)"),
    ("ape", rf"(?:(?<={_SEP})|^)ape(?={_SEP}|$)"),
    ("mp3", r"(?<![a-z])mp3(?![a-z])"),
    ("m4a", r"(?<![a-z])m4a(?![a-z])"),
    ("aac", r"(?<![a-z])aac(?![a-z])"),
    ("opus", rf"(?:(?<={_SEP})|^)opus(?={_SEP}|$)"),
    ("ogg", r"(?<![a-z])(?:ogg|vorbis)(?![a-z])"),
    ("wma", r"(?<![a-z])wma(?![a-z])"),
)
_CODECS = tuple((name, re.compile(pattern, re.I)) for name, pattern in _CODEC_PATTERNS)

# DSD states a family, not a resolution: DSD64 is 2.8224 MHz, and each step
# doubles it. Posts write either spelling, and the MHz one rounds ('2.8MHz',
# '5.6448 MHz')
_DSD_FAMILY = re.compile(r"(?<![a-z])dsd\s*[-_. ]?\s*(64|128|256|512)(?![\d])", re.I)
_DSD_MHZ = re.compile(r"(?<![\d.])(2\.8|5\.6|11\.2|22\.5)\d*\s*[-_. ]?\s*mhz(?![a-z])", re.I)
_MHZ_FAMILY = {"2.8": 64, "5.6": 128, "11.2": 256, "22.5": 512}
# an SACD RIP — 'SACD-R', 'SACD ISO' — is the disc's own 1-bit stream, and the
# SACD spec is DSD64 and nothing else. Bare 'SACD' is not: every SACD title in
# the corpus ('24bit 88Khz PS3 SACD', 'SHM SACD Hybrid Release') labels a PCM
# transfer of that disc, so it stays what it already was, a lossless claim
_SACD_IMAGE = re.compile(r"(?<![a-z])sacd[-_. ]?(?:r|iso)(?![a-z])", re.I)

_DEPTH = re.compile(_NUM + r"(8|16|20|24|32)\s*[-_. ]?\s*bits?(?![a-z])", re.I)
_RATE = re.compile(_NUM + r"(\d{2,3}(?:\.\d)?)\s*[-_. ]?\s*k?hz(?![a-z])", re.I)
# the bare pair, '24-96' or reversed '48-24' — no depth is a valid rate and no
# rate is a valid depth, so which number is which needs no separate pattern
_PAIR = re.compile(_NUM + r"(\d{2,3})[-_. ](\d{2,3})(?![\d])")
_KBPS = re.compile(_NUM + r"(\d{2,3})\s*k(?:bps|b/s|bit|b)(?![a-z])", re.I)
# a bare '320' in the tag soup is the MP3 bitrate; a catalogue number is longer
# and a track number shorter, so the digit lookarounds carry the distinction
_BARE_KBPS = re.compile(_NUM + r"(320|256|128)(?![\d])")
# a LAME preset names its encoder: V0 is an MP3, whatever else the title omits
_LAME_PRESET = re.compile(r"(?<![a-z])v[0-9](?![a-z0-9])", re.I)
_LOSSY_CLAIM = re.compile(r"(?<![a-z])(?:cbr|vbr|kbps|lame)(?![a-z])", re.I)
_LOSSLESS_CLAIM = re.compile(
    r"(?<![a-z])(?:lossless|hi[-. ]?res|hdtracks|qobuz|sacd|dvd[-. ]?a(?:udio)?)"
    r"(?![a-z])", re.I)

_EXTENSION_CODEC = {
    ".flac": "flac", ".alac": "alac", ".m4a": "m4a", ".mp3": "mp3", ".aac": "aac",
    ".ogg": "ogg", ".oga": "ogg", ".opus": "opus", ".wav": "wav", ".aif": "aiff",
    ".aiff": "aiff", ".ape": "ape", ".wv": "wv", ".dsf": "dsd", ".dff": "dsd",
    ".wma": "wma",
}

_CODEC_SCORE = {
    "flac": 88, "alac": 86, "ape": 82, "wv": 82, "wav": 80, "aiff": 80, "dsd": 78,
    "aac": 55, "m4a": 55, "opus": 55, "ogg": 52, "mp3": 50, "wma": 40,
}
# the title claims a class without naming a codec: 'JAPAN LOSSLESS', '(320kb)'
_CLAIMED_LOSSLESS_SCORE = 80.0
_CLAIMED_LOSSY_SCORE = 45.0
_UNKNOWN_SCORE = 30.0

_DEPTH_POINTS = {8: -6, 16: 0, 20: 2, 24: 4, 32: 5}
_RATE_POINTS = {44.1: 0, 48.0: 1, 64.0: 2, 88.2: 3, 96.0: 3,
                176.4: 4, 192.0: 4, 352.8: 5, 384.0: 5}
# DSD orders against DSD; the points stay small enough that no DSD ever reaches
# the score of a plain FLAC (see rank)
_DSD_POINTS = {64: 0, 128: 2, 256: 3, 512: 4}

# the resolutions a library can be capped at, worst to best; a release above the
# chosen one is excluded, not merely outranked
CEILINGS: dict[str, tuple[int, float] | None] = {
    "16_44": (16, 44.1),
    "24_48": (24, 48.0),
    "24_96": (24, 96.0),
    "24_192": (24, 192.0),
    "any": None,
}


@dataclass(frozen=True)
class Quality:
    codec: str | None = None
    bit_depth: int | None = None
    sample_rate_khz: float | None = None
    bitrate_kbps: int | None = None
    # what the title claimed when it named no codec at all
    lossless: bool | None = None
    # the DSD family: 64, 128, 256 or 512, DSD's own scale and never a PCM one
    dsd: int | None = None


def is_lossless(quality: Quality) -> bool | None:
    if quality.codec:
        return quality.codec in LOSSLESS_CODECS
    return quality.lossless


def parse_title(title: str) -> Quality:
    codec = next((name for name, pattern in _CODECS if pattern.search(title)), None)
    if codec is None and _SACD_IMAGE.search(title):
        codec = "dsd"
    if codec == "dsd":
        # 1 bit at 2.8 MHz is not a rung on the PCM ladder, so a depth or a rate
        # the same title states belongs to something else — the disc's PCM
        # layer, an earlier transfer — and is not this release's resolution
        return Quality("dsd", dsd=_dsd_family(title))
    depth = rate = None
    match = _DEPTH.search(title)
    if match:
        depth = int(match.group(1))
    for match in _RATE.finditer(title):
        rate = _rate_khz(float(match.group(1)))
        if rate:
            break
    if depth is None or rate is None:
        depth, rate = _pair(title, depth, rate)

    if codec in LOSSLESS_CODECS or depth is not None or _LOSSLESS_CLAIM.search(title):
        return Quality(codec, depth, rate, lossless=True if codec is None else None)
    if codec is None and _LAME_PRESET.search(title):
        codec = "mp3"
    bitrate = _kbps(title)
    lossy = codec is not None or bitrate is not None or bool(_LOSSY_CLAIM.search(title))
    return Quality(codec, depth, rate, bitrate,
                   lossless=False if lossy and codec is None else None)


def from_file(extension: str, bitrate_kbps: int | None = None) -> Quality:
    codec = _EXTENSION_CODEC.get(extension.lower())
    if codec in LOSSLESS_CODECS:
        # a FLAC's kbps is a property of the music, not of the rip
        bitrate_kbps = None
    return Quality(codec, bitrate_kbps=bitrate_kbps)


def merge(*parts: Quality) -> Quality:
    """One quality out of several readings of the same release, earlier
    evidence winning per field: the file itself settles the codec, the title is
    the only thing that ever states a depth or a rate."""
    values = {}
    for part in parts:
        for field in fields(Quality):
            if values.get(field.name) is None:
                values[field.name] = getattr(part, field.name)
    # DSD is the one codec a file list cannot state: a usenet post carries no
    # extension of its own and its indexer category reads Audio/Lossless, which
    # maps to FLAC. Only the title ever says DSD, so when it does, it wins.
    if any(part.codec == "dsd" for part in parts):
        values["codec"] = "dsd"
        values["dsd"] = next((p.dsd for p in parts if p.dsd), values.get("dsd"))
    return Quality(**values)


def rank(quality: Quality) -> tuple:
    """Sort key placing every quality on one line, best last. Unknown sits
    between lossy and lossless and below any stated resolution — it is unknown,
    not high.

    DSD is lossless, but a lane of its own INSIDE lossless and under every PCM
    rip, 24/192 included. It is not a higher-resolution FLAC; it is a different
    representation, and the one thing it wins here is the player's native DSD
    path for stereo. Against that stands everything a library copy has to do:
    DSF carries ID3 rather than Vorbis comments, DSDIFF makes tagging optional
    in its own spec, and nothing else in the chain reads either. So where both
    exist the FLAC is the better copy, and a DSD rip is what to take when there
    is no lossless PCM at all — never discarded, never automatically preferred.
    Its 2.8 MHz never enters the PCM columns; the family orders DSD against
    DSD and nothing else."""
    lossless = is_lossless(quality)
    tier = 2 if lossless else 1 if lossless is None else 0
    return (tier, _CODEC_SCORE.get(quality.codec, 0), quality.bit_depth or 0,
            quality.sample_rate_khz or 0.0, quality.dsd or 0,
            quality.bitrate_kbps or 0)


def score(quality: Quality) -> float:
    """0..100 quality axis for the candidate blend."""
    if quality.codec in _CODEC_SCORE:
        base = float(_CODEC_SCORE[quality.codec])
    elif quality.lossless is True:
        base = _CLAIMED_LOSSLESS_SCORE
    elif quality.lossless is False:
        base = _CLAIMED_LOSSY_SCORE
    else:
        return _UNKNOWN_SCORE
    if is_lossless(quality):
        if quality.codec == "dsd":
            return base + _DSD_POINTS.get(quality.dsd, 0)
        if quality.bit_depth is None and quality.sample_rate_khz is None:
            return base
        return min(100.0, base + 1 + _DEPTH_POINTS.get(quality.bit_depth, 0)
                   + _RATE_POINTS.get(quality.sample_rate_khz, 0))
    return max(0.0, min(100.0, base + _bitrate_points(quality.bitrate_kbps)))


def exceeds_ceiling(quality: Quality, ceiling: str) -> bool:
    limit = CEILINGS[ceiling]
    if limit is None:
        return False
    if quality.codec == "dsd":
        # 1-bit DSD has no PCM depth or rate to compare against; a library that
        # caps PCM resolution has no room for it either
        return True
    max_depth, max_rate = limit
    return ((quality.bit_depth is not None and quality.bit_depth > max_depth)
            or (quality.sample_rate_khz is not None
                and quality.sample_rate_khz > max_rate))


def _dsd_family(title: str) -> int | None:
    match = _DSD_FAMILY.search(title)
    if match:
        return int(match.group(1))
    match = _DSD_MHZ.search(title)
    if match:
        return _MHZ_FAMILY[match.group(1)]
    return 64 if _SACD_IMAGE.search(title) else None


def _rate_khz(value: float) -> float | None:
    # '44', '44.1' and '44.1kHz' are one rate — the integer part names the family
    return _RATES.get(int(value))


def _pair(title: str, depth: int | None, rate: float | None) -> tuple[int | None, float | None]:
    for first, second in _PAIR.findall(title):
        first, second = int(first), int(second)
        if first in _DEPTHS and second in _RATES:
            return depth or first, rate or _RATES[second]
        if first in _RATES and second in _DEPTHS:
            return depth or second, rate or _RATES[first]
    return depth, rate


def _kbps(title: str) -> int | None:
    match = _KBPS.search(title) or _BARE_KBPS.search(title)
    if match and 32 <= int(match.group(1)) <= 320:
        return int(match.group(1))
    return None


def _bitrate_points(bitrate_kbps: int | None) -> float:
    if bitrate_kbps is None:
        return 0.0
    if bitrate_kbps >= 320:
        return 10.0
    if bitrate_kbps >= 256:
        return 5.0
    if bitrate_kbps >= 192:
        return 0.0
    return -15.0
