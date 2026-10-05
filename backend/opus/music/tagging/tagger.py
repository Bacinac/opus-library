"""Custom tagger/importer: writes tags straight from the canonical catalog
(no MusicBrainz, no beets) and files tracks into the library layout defined
by the naming_template setting, e.g. {artist}/{album} ({year})/{nn} - {title}."""

import logging
import re
from pathlib import Path
from dataclasses import dataclass
from types import SimpleNamespace

import mutagen
from mutagen import MutagenError
from mutagen.apev2 import BINARY, APEValue
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK
from mutagen.mp4 import MP4, MP4Cover

from opus.music.library.matching import match_tracks
from opus.music.textnorm import safe_filename

log = logging.getLogger("opus.music.tagging")

# what OPUS · Library can read, tag and file — and therefore the only thing a download
# channel is allowed to offer, so a candidate can never arrive unimportable.
# DSD belongs here: the player in this chain decodes it natively, so a DSF/DFF
# rip is a real acquisition rather than a curiosity
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".dsf", ".dff",
                    ".ape", ".wv", ".wav", ".aiff", ".aif"}

# `codec` (the bare suffix probe_file records, no dot) for the two DSD
# containers — the one pair of formats that is a different EDITION of a
# record rather than a better or worse copy of the stereo one, wherever a
# file's format decides that (edition_of, the import supersede check)
DSD_CODECS = {"dsf", "dff"}

# containers whose tags are raw ID3: mutagen offers no easy interface for any
# of them and answers a plain string with "not a Frame instance", so their tags
# are written as the frames they are. MP3 keeps the same dialect
_ID3_CONTAINERS = {".mp3", ".dsf", ".dff", ".wav", ".aiff", ".aif"}
# APEv2 tag containers, which take a cover as a binary value rather than a frame
_APEV2_CONTAINERS = {".ape", ".wv"}
_ID3_FRAMES = {"albumartist": "TPE2", "artist": "TPE1", "album": "TALB",
               "title": "TIT2", "tracknumber": "TRCK"}


class ImportError_(Exception):
    pass


def _tag_reader(audio):
    """One reader for both tag dialects. A file whose tags are raw ID3 — every
    DSF, every tagged DSDIFF — answers nothing to the easy keys, so its frames
    are fetched by id; an untagged file answers None on every key, exactly like
    an untagged FLAC, and raises nothing."""
    tags = audio.tags
    if isinstance(tags, ID3):
        def first(key: str) -> str | None:
            text = getattr(tags.get(_ID3_FRAMES[key]), "text", None)
            return str(text[0]) if text else None
    else:
        def first(key: str) -> str | None:
            values = audio.get(key)
            return str(values[0]) if values else None
    return first


def probe_file(path: Path) -> dict:
    """Tags + quality + size in one mutagen read — the attribute set of a
    library file row. Empty dict when the file is unreadable."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as exc:
        log.warning("unreadable audio file %s: %s: %s", path, type(exc).__name__, exc)
        return {}
    if audio is None:
        return {}

    first = _tag_reader(audio)
    tag_track = None
    raw_track = first("tracknumber")
    if raw_track:
        head = raw_track.split("/")[0].strip()
        if head.isdigit():
            tag_track = int(head)

    info = audio.info
    bitrate = getattr(info, "bitrate", 0) if info else 0
    try:
        stat = path.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        size, mtime = None, None
    # Everything the file says, kept beside the handful of fields the catalogue
    # reads. It is not a second opinion about what a record is called — the
    # catalogue answers that — it is a record of what THIS FILE said when it was
    # last opened, which is the only way to know what a tag write would change
    # without opening forty-eight thousand files to find out. `mtime` and `size`
    # on the same row say when it went stale.
    tags = {}
    for key, values in (getattr(audio, "tags", None) or {}).items():
        held = values if isinstance(values, list) else [values]
        text = [str(v) for v in held if str(v)]
        if text:
            tags[key.upper()] = text[0] if len(text) == 1 else text

    return {
        "size": size,
        "mtime": mtime,
        "tags": tags,
        "tag_artist": first("albumartist") or first("artist"),
        "tag_album": first("album"),
        "tag_title": first("title"),
        "tag_track": tag_track,
        "duration_sec": round(info.length) if info and getattr(info, "length", None) else None,
        "codec": path.suffix.lower().lstrip("."),
        "bitrate_kbps": round(bitrate / 1000) if bitrate else None,
        "sample_rate_hz": getattr(info, "sample_rate", None) if info else None,
        "bit_depth": getattr(info, "bits_per_sample", None) if info else None,
        "channels": getattr(info, "channels", None) if info else None,
    }


def render_track_path(template: str, artist: str, album: str, year: str | None,
                      position: int, title: str) -> Path:
    values = {
        "artist": safe_filename(artist),
        "album": safe_filename(album),
        "year": year or "",
        "nn": f"{position:02d}",
        "title": safe_filename(title),
    }
    out = template
    for token, value in values.items():
        out = out.replace("{" + token + "}", value)
    # a missing year leaves empty () / [] groups behind — drop them
    out = re.sub(r"[\(\[]\s*[\)\]]", "", out)
    out = re.sub(r"\s{2,}", " ", out)
    parts = [p.strip() for p in out.split("/") if p.strip()]
    if not parts:
        raise ImportError_(f"naming template rendered an empty path: {template!r}")
    return Path(*parts)


def _save_id3(path: Path, *frames):
    """Write ID3 frames into whichever container holds them, replacing the ones
    already there. A container that refuses the tag names itself: DSDIFF makes
    ID3 optional in its own spec, so a rip that will not take one has to say so
    rather than pass for tagged."""
    audio = mutagen.File(path)
    if audio is None:
        raise ImportError_(f"unsupported audio file: {path}")
    if audio.tags is None:
        audio.add_tags()
    for frame in frames:
        audio.tags.delall(frame.FrameID)
        audio.tags.add(frame)
    try:
        audio.save()
    except MutagenError as exc:
        raise ImportError_(f"cannot write tags to {path.name}: {exc}") from exc


def _write_tags(path: Path, artist: str, album: str, title: str, position: int,
                total: int, year: str | None):
    suffix = path.suffix.lower()
    if suffix == ".flac":
        audio = FLAC(path)
        audio["artist"] = artist
        audio["albumartist"] = artist
        audio["album"] = album
        audio["title"] = title
        audio["tracknumber"] = str(position)
        audio["tracktotal"] = str(total)
        if year:
            audio["date"] = year
        audio.save()
    elif suffix in _ID3_CONTAINERS:
        frames = [TPE1(encoding=3, text=artist), TPE2(encoding=3, text=artist),
                  TALB(encoding=3, text=album), TIT2(encoding=3, text=title),
                  TRCK(encoding=3, text=f"{position}/{total}")]
        if year:
            frames.append(TDRC(encoding=3, text=year))
        _save_id3(path, *frames)
    else:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            raise ImportError_(f"unsupported audio file: {path}")
        audio["artist"] = artist
        audio["albumartist"] = artist
        audio["album"] = album
        audio["title"] = title
        if year:
            audio["date"] = year
        if isinstance(audio, EasyMP4):
            # MP4 keeps position and total in one atom and has no separate
            # total key; Vorbis comments and APEv2 want the two apart
            audio["tracknumber"] = f"{position}/{total}"
        else:
            audio["tracknumber"] = str(position)
            audio["tracktotal"] = str(total)
        audio.save()


def _embed_cover(path: Path, image_bytes: bytes, mime: str):
    suffix = path.suffix.lower()
    if suffix == ".flac":
        audio = FLAC(path)
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = mime
        picture.data = image_bytes
        audio.clear_pictures()
        audio.add_picture(picture)
        audio.save()
    elif suffix in _ID3_CONTAINERS:
        _save_id3(path, APIC(encoding=3, mime=mime, type=3, desc="Cover",
                             data=image_bytes))
    elif suffix == ".m4a":
        audio = MP4(path)
        image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(image_bytes, imageformat=image_format)]
        audio.save()
    elif suffix in _APEV2_CONTAINERS:
        audio = mutagen.File(path)
        if audio is None:
            raise ImportError_(f"unsupported audio file: {path}")
        if audio.tags is None:
            audio.add_tags()
        # APEv2 carries the picture as filename, a NUL, then the image bytes
        audio.tags["Cover Art (Front)"] = APEValue(
            _image_filename("cover", mime).encode() + b"\x00" + image_bytes, BINARY)
        audio.save()
    # ogg/opus get the file-level cover only (base64 metadata blocks not worth it)


def _image_filename(base: str, mime: str) -> str:
    return f"{base}.png" if mime == "image/png" else f"{base}.jpg"


def retag_file(path: Path, artist: str, album: str, title: str, position: int,
               total: int, year: str | None):
    """Align every catalog-backed tag of an existing library file — the
    album-level 'retag all' action (title, numbering, album/artist, date)."""
    _write_tags(path, artist, album, title, position, total, year)


def write_title_tag(path: Path, title: str):
    """Correct a file's title tag to the catalog title (mtime moves so tag
    scanners — DIDA's medialib — pick the change up)."""
    if path.suffix.lower() in _ID3_CONTAINERS:
        _save_id3(path, TIT2(encoding=3, text=title))
        return
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ImportError_(f"unsupported audio file: {path}")
    audio["title"] = title
    audio.save()


def write_album_tag(path: Path, album: str):
    """Correct a file's album tag, the way `write_title_tag` corrects its
    title. The two are separate because a record's name changing is not its
    songs changing, and rewriting every tag on a file to move one of them is
    how a stray edit gets into the others."""
    if path.suffix.lower() in _ID3_CONTAINERS:
        _save_id3(path, TALB(encoding=3, text=album))
        return
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ImportError_(f"unsupported audio file: {path}")
    audio["album"] = album
    audio.save()


def write_folder_image(dir_path: Path, image_bytes: bytes, mime: str):
    """Artist-level portrait, media-server convention (folder.jpg)."""
    target = dir_path / _image_filename("folder", mime)
    if not target.exists():
        target.write_bytes(image_bytes)


@dataclass
class ImportPlan:
    """What an import WOULD do, decided before a single file moves. The caller
    rules on it — a download that cannot complete the album, or that would
    replace better files, is discarded by simply never applying its plan."""

    source_dir: Path
    audio_files: list[Path]
    rows: list          # probed file rows, shaped for the library matcher
    pairs: dict[int, int]  # {track_id: index into audio_files}

    @property
    def matched(self) -> int:
        return len(self.pairs)

    @property
    def unclaimed(self) -> list[Path]:
        """Audio the plan matched to nothing — a different edition's extra
        tracks, or a bonus disc the catalog does not list."""
        taken = set(self.pairs.values())
        return [p for i, p in enumerate(self.audio_files) if i not in taken]


def plan_import(source_dir: Path, tracks: list) -> ImportPlan:
    """Probe the download and match it against the catalog tracks WITHOUT
    touching anything. Matching runs through the library matcher, so an import
    decides exactly the way a scan of the same folder would: scene releases
    name files after the release, not the song, while their tags are correct."""
    audio_files = sorted(
        p for p in source_dir.rglob("*") if p.suffix.lower() in AUDIO_EXTENSIONS
    )
    if not audio_files:
        image = next((p for p in sorted(source_dir.rglob("*"))
                      if p.suffix.lower() == ".iso"), None)
        if image:
            raise ImportError_(
                f"{image.name} is a disc image, not audio: an SACD/DVD-Audio "
                f"ISO has to be extracted to DSF or FLAC outside OPUS · Library before "
                f"it can be imported")
        raise ImportError_(f"no audio files found in {source_dir}")
    rows = []
    for index, path in enumerate(audio_files):
        info = probe_file(path)
        rows.append(SimpleNamespace(
            id=index, path=str(path), tag_title=info.get("tag_title"),
            tag_track=info.get("tag_track"), duration_sec=info.get("duration_sec"),
            channels=info.get("channels"), codec=info.get("codec"),
        ))
    return ImportPlan(source_dir=source_dir, audio_files=audio_files, rows=rows,
                      pairs=match_tracks(tracks, rows))


def plan_for(plan: ImportPlan, tracks: list) -> ImportPlan:
    """The same probed files matched against a different tracklist — used when
    an adopted edition replaces the catalog's rows and the pairing has to be
    redecided without reading every file again."""
    return ImportPlan(source_dir=plan.source_dir, audio_files=plan.audio_files,
                      rows=plan.rows, pairs=match_tracks(tracks, plan.rows))


def edition_of(channels: int | None, codec: str | None = None) -> str:
    """What to call a copy that is not the ordinary one, and the folder it is
    filed into. Stereo FLAC is the album as everyone has it and gets no name;
    anything wider is a different record of the same music and lives beside it
    under the name of its own layout; DSD is the same music again, in the one
    format the DAC plays natively, filed in a folder of its own so it never
    shares a name — or an edition key — with the ordinary copy."""
    if channels and channels > 2:
        return {4: "4.0", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}.0")
    if (codec or "").lower() in DSD_CODECS:
        return "dsd"
    return ""


def prepare_import(plan: ImportPlan, files, artist_name: str, album_title: str,
                 release_date: str | None, tracks: list,
                 music_dir: str, naming_template: str,
                 cover_bytes: bytes | None = None,
                 cover_mime: str = "image/jpeg",
                 edition: str = "") -> dict[int, str]:
    """Copy and tag every planned file before publishing any library path.

    An edition names a folder inside the album's own: the surround mix of a
    record is not a better copy of the stereo one and must not be written over
    it, and two editions rendered from one naming template are one path."""
    year = release_date[:4] if release_date else None
    # the plan was drawn before the edition checks, which cost network calls —
    # long enough for the download client to rename or clear the job folder
    # underneath it. Say that, instead of letting the first move raise a bare
    # ENOENT naming a path nobody can place
    gone = [plan.audio_files[row_id].name
            for row_id in plan.pairs.values()
            if not plan.audio_files[row_id].exists()]
    if gone:
        raise ImportError_(
            f"{len(gone)} planned file(s) disappeared from {plan.source_dir} "
            f"before the import could move them, starting with {gone[0]!r} — "
            f"the download client cleared the job"
        )
    imported: dict[int, str] = {}
    for track in tracks:
        row_id = plan.pairs.get(track.id)
        if row_id is None:
            continue
        best_path = plan.audio_files[row_id]
        relative = render_track_path(
            naming_template, artist_name, album_title, year, track.position, track.title
        )
        if edition:
            relative = relative.parent / edition / relative.name
        # append the extension rather than with_suffix(): titles may contain dots
        dest = Path(music_dir) / relative.parent / (relative.name + best_path.suffix.lower())
        staged = files.copy(best_path, dest)
        _write_tags(staged, artist_name, album_title, track.title,
                    track.position, len(tracks), year)
        if cover_bytes:
            _embed_cover(staged, cover_bytes, cover_mime)
        imported[track.id] = str(dest)

    if not imported:
        raise ImportError_(
            f"no files in {plan.source_dir} matched any of "
            f"{len(tracks)} catalog tracks"
        )
    if cover_bytes:
        # album cover file, media-server convention (cover.jpg)
        for album_dir in {Path(p).parent for p in imported.values()}:
            files.reserve(album_dir / _image_filename("cover", cover_mime)).write_bytes(cover_bytes)
    return imported
