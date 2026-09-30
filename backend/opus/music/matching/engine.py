"""Matching engine: score chaotic channel candidates against the canonical
release. Scoring axes: album completeness, measured quality (opus.music.matching.
quality), peer speed/queue.

A whole-album usenet post exposes nothing but its title, so judging it is the
delicate part. title_score is built on token_set_ratio, which returns 100
whenever the album's tokens are a SUBSET of the post's — so 'Iron Maiden'
scored 100 against 'Iron.Maiden-Killers' and against a 71 GB discography, and
'The Dubliners' scored 100 against 'Seven Drunken Nights'. Every post of an
artist tied at the top and the pipeline downloaded whichever the indexer
happened to list first. Whole-album posts therefore get their own measure —
the artist must OPEN the title, every album token must be present, and what
is left over counts against the fit — plus outright rejects for the shapes
that are never one album.

A post that carries MORE than one album — a discography, an anthology, a box —
is a third case. Its title neither names the album nor rules it out, so it is
scored on neither: it enters the field on the artist alone, ranked under every
post whose title does name the album, and only a file list can settle it
(choose._confirmed_best reads the manifest). Once the files are known it is
an ordinary candidate, judged on the ones that answer THIS album's tracklist
and on nothing else — a box's other albums are a different rip and their bytes
are not this album's size."""

import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from opus.music.channels.base import Candidate, CandidateFile
from opus.music.matching import quality
from opus.music.textnorm import TITLE_MATCH_THRESHOLD, artist_key, norm, title_score

log = logging.getLogger("opus.music.matching")

# a whole-album release should hold roughly this many bytes per track; a stray
# single-track NZB is an order of magnitude smaller, so its album-title match is
# scaled down and it can't masquerade as the full album. Only the title-judged
# branch needs it: a known file list answers the same question by counting.
_ALBUM_BYTES_PER_TRACK = 12 * 1024 * 1024

# what a multi-album post is worth before its file list is read — enough to stay
# in the field and be opened, never enough to outrank a post whose title names
# the album, and worthless on its own (choose._confirmed_best never takes an
# unproven candidate)
_UNPROVEN_FIT = 0.5


def apply_quality_ceiling(scored: list["ScoredCandidate"], ceiling: str) -> list["ScoredCandidate"]:
    kept = [s for s in scored if not quality.exceeds_ceiling(s.quality, ceiling)]
    _report_emptied(scored, kept, f"the {ceiling} quality ceiling")
    return kept


def apply_quality_profile(scored: list["ScoredCandidate"], profile: str) -> list["ScoredCandidate"]:
    if profile == "lossless_only":
        kept = [s for s in scored if quality.is_lossless(s.quality)]
        _report_emptied(scored, kept, "the lossless_only profile")
        return kept
    if profile == "prefer_lossless":
        lossless = [s for s in scored if quality.is_lossless(s.quality)]
        return lossless or scored
    return scored


def _report_emptied(before: list["ScoredCandidate"], after: list["ScoredCandidate"],
                    rule: str) -> None:
    """A setting that turns a field of real candidates into nothing looks
    exactly like a search that found nothing. Say which rule did it, or the
    album reads as unavailable when it is only filtered out."""
    if before and not after:
        log.warning("%s left no candidates out of %d: %s", rule, len(before),
                    ", ".join(s.candidate.title for s in before[:3]))


@dataclass
class ScoredCandidate:
    candidate: Candidate
    score: float
    completeness: float
    quality_score: float
    quality: quality.Quality
    # the post's title says it carries more than this album; what it really
    # holds is knowable only from its file list
    multi_album: bool = False

    @property
    def unproven(self) -> bool:
        """A box read off its title alone. Nothing there says the album is
        inside, so it may not be downloaded until a file list says so."""
        return self.multi_album and self.candidate.whole_album


def _quality(candidate: Candidate, files: list[CandidateFile]) -> quality.Quality:
    """What the candidate states about its own quality. The file settles the
    codec — a real extension on slskd, the indexer's own category on a usenet
    post, both steadier than the title — while the title is the only place a
    depth or a rate is ever written. An album is worth its worst file: a lone
    MP3 among the FLACs makes the whole folder lossy, which is what the import
    would rule anyway. The files are the album's own — a box set's other discs
    may be a different rip entirely and do not speak for this one."""
    stated = quality.parse_title(candidate.title)
    return min(
        (quality.merge(quality.from_file(f.extension, f.bitrate), stated)
         for f in files),
        key=quality.rank,
        default=stated,
    )


def _artist_prefix(title: str, artist: str) -> int | None:
    """How many leading tokens the artist name consumes, or None when it does
    not open the title at all. Usenet titles read Artist-Album-tags, so the
    artist stands at the FRONT.
    One appearing later is a tribute, a remix or a compilation ('Kash Vs. INXS',
    'The Iron Maidens', 'VA-Tribute to…'). Matched exactly after folding, the
    same strictness identity uses everywhere else — fuzzy variants let 'Dope
    Queen 707' through for 'Queen'."""
    key = artist_key(artist).replace(" ", "")
    if not key:
        return None
    tokens = norm(title).split()
    consumed = 0
    while tokens and tokens[0].isdigit():  # scene index prefix: '1836-Guns.N.Roses…'
        tokens, consumed = tokens[1:], consumed + 1
    if tokens and tokens[0] == "the" and not key.startswith("the"):
        tokens, consumed = tokens[1:], consumed + 1
    accumulated = ""
    used = 0
    for token in tokens[:5]:
        accumulated += token
        used += 1
        if len(accumulated) >= len(key):
            break
    return consumed + used if accumulated == key else None


# shapes that are more than one album, whatever else the title says
_NOT_ONE_ALBUM = re.compile(
    # separators are whatever the scene felt like: 'Greatest.Hits', 'Best_Of'.
    # The year range must be two plausible years in order — a bare \d{4}-\d{4}
    # also reads Warner's catalogue number '8122-79657' and the scene's own
    # 'Rush-1976-2112' as a discography
    r"discograph|antholog|complete\b|collection|greatest[.\s_-]*hits|"
    r"best[.\s_-]*of|box[.\s_-]*set|"
    r"(?:19|20)\d{2}\s*-\s*(?:19|20)\d{2}", re.IGNORECASE)
_PART_DISC = re.compile(r"\b(?:cd|disc|disk)\s*0?[1-9]\b", re.IGNORECASE)
# a numeral straight after the album name is a different record ('Queen II')
_SEQUEL_MARKERS = {"ii", "iii", "iv", "v", "vi", "vii", "2", "3", "4", "5", "6",
                   "vol", "volume", "part", "pt"}
# scene/format furniture that says nothing about WHICH album this is
_NOISE = re.compile(
    # both spellings of a resolution: '24-96' splits into bare numbers while
    # '24BIT-192KHZ' stays glued, and a post must not be marked down for
    # writing its own quality the second way
    r"^(?:flac|mp3|web|\d*cd|cda|vinyl|lp|ep|8|16|20|24|32|"
    # every rate family quality._RATES knows, since a post writes one of them
    r"44|48|64|88|96|176|192|352|384|bit|khz|kbps|"
    r"\d+bit|\d+khz|\d+kbps|"
    r"remaster|remastered|reissue|deluxe|edition|expanded|limited|special|"
    r"anniversary|mono|stereo|shm|sacd|hdtracks|qobuz|proper|repack|retail|"
    r"\d{4})$", re.IGNORECASE)


def _album_fit(candidate: Candidate, album_title: str, artist: str) -> float:
    """0..1 fit of a whole-album post to THIS album. Every album token must be
    present in what remains of the title after the artist (recall, not
    similarity — 'Killers' shares no token with 'Iron Maiden' and is rejected
    outright instead of tying at 100), and each leftover token costs a little,
    so a plain rip outranks one carrying a second album's name."""
    prefix = _artist_prefix(candidate.title, artist)
    if prefix is None:
        return 0.0
    residue = norm(candidate.title).split()[prefix:]
    wanted = norm(album_title).split()
    if not wanted:
        return 0.0
    # an album title that embeds the artist ('Best Of The Byrds') is written
    # tersely on the post, which already opens with the name — ask only for
    # what the title adds. A SELF-titled album adds nothing, and there the
    # name repeating after the prefix is exactly the signature to look for.
    # what remains must also SAY something: 'Bad Company (Deluxe)' reduces to
    # the bare word 'deluxe', which fits every deluxe edition the artist ever
    # had, so an edition qualifier alone falls back to demanding the full title
    artist_tokens = set(norm(artist).split())
    distinct = [t for t in wanted
                if t not in artist_tokens and not _NOISE.match(t)]
    wanted = distinct or wanted
    # recall runs against the RAW residue: cleaning first would delete the
    # numerals of an album that IS numeric (Sex Pistols '76-77')
    used: set[int] = set()
    matched_at = -1
    for token in wanted:
        hit = next((i for i, r in enumerate(residue)
                    if i not in used and fuzz.ratio(token, r) >= 90), None)
        if hit is None:
            return 0.0
        matched_at = max(matched_at, hit)
        used.add(hit)
    # a NUMERAL straight after the name is a different record ('Queen II').
    # A word is not: reissues extend the name ('OK Computer OKNOTOK', 'Tommy
    # Deluxe Edition') and rejecting on it threw away one post in ten that
    # named its own album. An unexplained word costs a leftover instead, which
    # is enough — 'Buffalo Springfield Again' fits at 0.92 and loses to the
    # album's own post at 1.00.
    following = residue[matched_at + 1:matched_at + 2]
    if following and following[0] in _SEQUEL_MARKERS:
        return 0.0
    leftovers = [t for i, t in enumerate(residue)
                 if i not in used and not _NOISE.match(t) and len(t) > 1]
    return max(0.0, 1.0 - 0.08 * len(leftovers))


def _holds_more_than_the_album(title: str, album_title: str | None) -> bool:
    """Whether the post says it carries several albums — a discography, an
    anthology, a box. …unless the album IS one of those things: 'Greatest Hits'
    and 'Best Of The Byrds' are records in their own right, and reading their
    own name as a box would make them unobtainable."""
    return bool(_NOT_ONE_ALBUM.search(title)
                and not (album_title and _NOT_ONE_ALBUM.search(album_title)))


def _title_completeness(candidate: Candidate, track_titles: list[str],
                        album_title: str | None, artist_name: str | None,
                        multi_album: bool) -> float:
    """A whole-album release (a usenet NZB) exposes only a title, not a file
    list — how well that title fits the album, scaled by whether the release is
    actually album-sized (which guards against single-track NZBs)."""
    if not album_title or not artist_name:
        return 0.0
    if _artist_prefix(candidate.title, artist_name) is None:
        return 0.0
    if _PART_DISC.search(candidate.title):
        return 0.0
    if multi_album:
        return _UNPROVEN_FIT
    match = _album_fit(candidate, album_title, artist_name)
    if not match:
        return 0.0
    total = sum(f.size for f in candidate.files)
    expected = max(1, len(track_titles)) * _ALBUM_BYTES_PER_TRACK
    size_factor = min(1.0, total / expected) if total else 1.0
    return match * size_factor


def _listed_completeness(
        files: list[CandidateFile],
        track_titles: list[str]) -> tuple[float, list[CandidateFile]]:
    """How much of the album a real file list answers, and the files that
    answered it. Every track needs a file of its OWN — two tracks landing on
    one name are one track delivered, not two, which is what keeps a box set's
    hundreds of names from lending recall to an album it does not hold. Only
    the answering files speak for the release afterwards; the rest of a box is
    a neighbouring album's business, not this one's."""
    if not track_titles:
        return 0.0, []
    # strongest pairing first, each track and each file spent once: taking the
    # per-track best in list order let an early track claim a file a later one
    # matched better, leaving that later track empty while a file it could have
    # had went unused — a complete post reading as short of the album
    pairs = sorted(
        ((title_score(title, file.name), track_index, file_index)
         for track_index, title in enumerate(track_titles)
         for file_index, file in enumerate(files)),
        key=lambda pair: (-pair[0], pair[1], pair[2]),
    )
    taken_tracks: set[int] = set()
    claimed: dict[int, CandidateFile] = {}
    for score, track_index, file_index in pairs:
        if score < TITLE_MATCH_THRESHOLD:
            break
        if track_index in taken_tracks or file_index in claimed:
            continue
        taken_tracks.add(track_index)
        claimed[file_index] = files[file_index]
    return len(claimed) / len(track_titles), list(claimed.values())


def _speed_score(candidate: Candidate) -> float:
    if candidate.queue_length:
        return max(0.0, 50.0 - candidate.queue_length * 10)
    if candidate.speed_bps is None:
        return 50.0
    return min(100.0, candidate.speed_bps / (1024 * 1024) * 20)


def score_candidates(candidates: list[Candidate], track_titles: list[str],
                     album_title: str | None = None,
                     artist_name: str | None = None,
                     drop_unmatched: bool = True) -> list[ScoredCandidate]:
    """Candidates ranked best-first. A candidate that matches NOTHING of the
    album is dropped here rather than scored low: format and speed alone are
    worth 40 points, which is the whole viability floor, so an unrelated
    lossless post used to pass it with completeness zero. A multi-album post
    survives without matching anything — its title cannot match — and is
    marked `unproven` for the caller to settle or leave.

    `drop_unmatched=False` keeps them anyway, completeness and all — the
    reading a human asks for when the automatic grab already tried this release
    and plainly none of what it found was good enough to take on its own."""
    scored = []
    for candidate in candidates:
        # only a whole-album post can BE several albums; a slskd candidate's
        # title is 'user:folder', where the words mean nothing of the sort
        multi_album = (candidate.whole_album
                       and _holds_more_than_the_album(candidate.title, album_title))
        if candidate.whole_album:
            completeness = _title_completeness(candidate, track_titles,
                                               album_title, artist_name,
                                               multi_album)
            files = candidate.files
        else:
            completeness, files = _listed_completeness(candidate.files,
                                                       track_titles)
        if completeness <= 0 and drop_unmatched:
            continue
        measured = _quality(candidate, files)
        quality_score = quality.score(measured)
        speed = _speed_score(candidate)
        total = 0.55 * completeness * 100 + 0.35 * quality_score + 0.10 * speed
        scored.append(ScoredCandidate(candidate, total, completeness,
                                      quality_score, measured, multi_album))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
