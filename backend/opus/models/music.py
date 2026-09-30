"""The music half of the library: who made a record, which records exist, which
songs are on them, and which files on disk are those songs.

The video half is in `opus.models.video`; the two share nothing but the base and
the settings table, which is the whole point — a movie is not a shorter album."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from opus.music.titlecase import display_title

from opus.models.base import Base


class ReleaseStatus(enum.StrEnum):
    NONE = "none"
    WANTED = "wanted"
    SEARCHING = "searching"
    DOWNLOADING = "downloading"
    COMPLETE = "complete"
    FAILED = "failed"


class MusicDownloadStatus(enum.StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    COMPLETE = "complete"
    FAILED = "failed"
    # the download arrived intact and was turned away on policy — a partial
    # replacement, a second mix, lossy audio under a lossless profile. Nothing
    # is broken, so it must not read as a fault
    REJECTED = "rejected"
    CANCELLED = "cancelled"


# statuses that end a download's life; everything else is still in flight
DOWNLOAD_SETTLED = (MusicDownloadStatus.COMPLETE, MusicDownloadStatus.FAILED,
                    MusicDownloadStatus.REJECTED, MusicDownloadStatus.CANCELLED)
# a post that ended any of these ways is never worth pulling for this release
# again — an unrepairable one cannot become repairable, and one the user
# stopped is one they did not want
DOWNLOAD_SPENT = (MusicDownloadStatus.FAILED, MusicDownloadStatus.REJECTED,
                  MusicDownloadStatus.CANCELLED)
# evidence that a SOURCE is dead for a release, which is a different question:
# a cancellation says the user did not want this transfer now, not that the
# channel has nothing — blacklisting the channel on it locked albums out for good
DEAD_SOURCE = (MusicDownloadStatus.FAILED, MusicDownloadStatus.REJECTED)

# the columns each catalogue keeps its ids in, so what moves or weighs a row's
# identity takes every one of them along
ARTIST_CATALOG_IDS = ("deezer_id", "tidal_id")
RELEASE_SOURCE_IDS = ("deezer_id", "tidal_id", "discogs_id", "spotify_id", "wikidata_id")
TRACK_CATALOG_IDS = ("deezer_id", "tidal_id")


class ChannelName(enum.StrEnum):
    SLSKD = "slskd"
    SABNZBD = "sabnzbd"
    TIDAL = "tidal"


class EnrichStatus(enum.StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    FAILED = "failed"


class Artist(Base):
    __tablename__ = "artists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    deezer_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    tidal_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    wikidata_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    artist_type: Mapped[str | None] = mapped_column(String(16))  # person | group
    country: Mapped[str | None] = mapped_column(String(64))
    country_hr: Mapped[str | None] = mapped_column(String(64))
    begin_year: Mapped[int | None] = mapped_column(Integer)
    end_year: Mapped[int | None] = mapped_column(Integer)
    bio: Mapped[str | None] = mapped_column(Text)
    bio_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    # false = shadow row created from a relation (e.g. band member), not in library
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    # the artist's Wikipedia studio-albums list — the main-table authority
    wiki_studio_albums: Mapped[list | None] = mapped_column(JSONB)
    # checkpoint of the last successful full enrich cycle — restarts resume
    # instead of re-walking every artist
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrich_status: Mapped[EnrichStatus] = mapped_column(
        Enum(EnrichStatus, values_callable=lambda e: [m.value for m in e]),
        default=EnrichStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    releases: Mapped[list["Release"]] = relationship(back_populates="artist", cascade="all, delete-orphan")
    external_ids: Mapped[list["ArtistExternalId"]] = relationship(
        back_populates="artist", cascade="all, delete-orphan"
    )


class ArtistExternalId(Base):
    __tablename__ = "artist_external_ids"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))  # spotify | discogs | musicbrainz | ...
    external_id: Mapped[str] = mapped_column(String(128))

    artist: Mapped[Artist] = relationship(back_populates="external_ids")


class ArtistRelation(Base):
    __tablename__ = "artist_relations"
    __table_args__ = (UniqueConstraint("artist_id", "related_artist_id", "relation"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # member_of: artist_id is the member, related_artist_id is the group
    artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    related_artist_id: Mapped[int] = mapped_column(ForeignKey("artists.id", ondelete="CASCADE"))
    relation: Mapped[str] = mapped_column(String(32), default="member_of")
    source: Mapped[str] = mapped_column(String(32), default="wikidata")


class TidalAccount(Base):
    """A linked Tidal account (device-flow OAuth, linked through the UI).
    Multiple people can link their own; the catalogue uses the first that
    works and fails over to the next."""

    __tablename__ = "tidal_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    # the only token kept: Tidal rotates it on every refresh, and an access
    # token is minted from it each time a session is needed
    refresh_token: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (UniqueConstraint("entity_type", "entity_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16))  # artist | release
    entity_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32))  # deezer | itunes | spotify | discogs | wikimedia
    url: Mapped[str] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    chosen: Mapped[bool] = mapped_column(Boolean, default=False)
    chosen_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[int] = mapped_column(primary_key=True)
    artist_id: Mapped[int] = mapped_column(
        ForeignKey("artists.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    deezer_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    tidal_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    spotify_id: Mapped[str | None] = mapped_column(String(64))
    discogs_id: Mapped[int | None] = mapped_column(BigInteger)
    wikidata_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    release_date: Mapped[str | None] = mapped_column(String(10))
    description: Mapped[str | None] = mapped_column(Text)
    # who put the record out, and what kind of music it is. Empty means asked
    # and there was nothing — the same convention the description uses.
    label: Mapped[str | None] = mapped_column(String(200))
    genres: Mapped[list | None] = mapped_column(JSONB)
    record_type: Mapped[str | None] = mapped_column(String(32))
    cover_url: Mapped[str | None] = mapped_column(Text)
    track_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[ReleaseStatus] = mapped_column(
        Enum(ReleaseStatus, values_callable=lambda e: [m.value for m in e]),
        default=ReleaseStatus.NONE,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The one place a title is written, whichever of the eight that create a
    # release or the several that correct one is doing the writing. Four
    # services disagree about capitals and none of that is a decision about
    # this library; the form is settled here, on the way in, so nothing
    # downstream has to remember to ask.
    @validates("title")
    def _titled(self, _key, value):
        return display_title(value) if value else value

    artist: Mapped[Artist] = relationship(back_populates="releases")
    tracks: Mapped[list["Track"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    downloads: Mapped[list["MusicDownload"]] = relationship(back_populates="release", cascade="all, delete-orphan")
    variants: Mapped[list["ReleaseVariant"]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )


class ReleaseVariant(Base):
    """The album also exists as a different MIX — Atmos, multichannel, the
    1-bit layer of an SACD — noticed in a payload some other search was
    already reading. Stereo is the implicit default and is never recorded.

    A footnote on the release, never a release of its own: a surround mix is
    not a better copy of the stereo record but a separate reading of it, so
    nothing here is downloaded and the release's own files and status are
    untouched. `external_id` is where the mix was seen at `source`, which is
    what a later decision to fetch it would start from."""

    __tablename__ = "release_variants"
    __table_args__ = (UniqueConstraint("release_id", "variant", "source"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # the unique constraint below leads with release_id, which is the only way
    # this table is ever read — a second index on it would be dead weight
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"))
    variant: Mapped[str] = mapped_column(String(16))  # atmos | multichannel | sacd
    source: Mapped[str] = mapped_column(String(32))  # a catalogue's name, or discogs
    external_id: Mapped[str | None] = mapped_column(String(128))
    seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    release: Mapped[Release] = relationship(back_populates="variants")


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(
        ForeignKey("releases.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    title_manual: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    @validates("title")
    def _titled(self, _key, value):
        return display_title(value) if value else value
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    deezer_id: Mapped[int | None] = mapped_column(BigInteger)
    tidal_id: Mapped[int | None] = mapped_column(BigInteger)

    release: Mapped[Release] = relationship(back_populates="tracks")
    files: Mapped[list["MusicFile"]] = relationship(back_populates="track")


class TrackLyrics(Base):
    """The words to a song, and when each line is sung.

    Kept beside the track rather than in the file, because the file is not
    always where they come from: a rip carries them about a third of the time
    and never with timings, and a line that arrives from elsewhere must not mean
    rewriting a library the importer treats as read-only. `synced` is LRC as it
    was received, `plain` the same words without timings — a track can have the
    second and not the first, and one whose search came back empty is written
    down as a miss so the next play does not ask the network again."""

    __tablename__ = "track_lyrics"

    track_id: Mapped[int] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
    synced: Mapped[str | None] = mapped_column(Text)
    plain: Mapped[str | None] = mapped_column(Text)
    # said by the source, and the one honest answer for a track with no words:
    # nothing found and nothing to find are not the same state
    instrumental: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false")
    source: Mapped[str] = mapped_column(String(16))  # file | sidecar | lrclib | none
    looked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())


class DiscogsMastersCache(Base):
    """An artist's main-masters listing — Discogs paginates the FULL release
    catalog at 60 req/min, which for mega-artists is 40+ pages; fetched once
    per week instead of once per process."""

    __tablename__ = "discogs_masters_cache"

    artist_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    masters: Mapped[list] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FolderScanCache(Base):
    """The deep-pass verdict for a library folder, keyed by relative path.
    The escalated multi-source hunt costs minutes of rate-limited API calls,
    and an unchanged folder against an unchanged artist catalog can only
    reach the same verdict — the fingerprint covers the folder's files, the
    artist's release list and the matcher version, so any change re-runs."""

    __tablename__ = "folder_scan_cache"

    folder: Mapped[str] = mapped_column(Text, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    artist_id: Mapped[int | None] = mapped_column(Integer)
    artist_name: Mapped[str | None] = mapped_column(String(500))
    album: Mapped[str | None] = mapped_column(String(500))
    matched: Mapped[int | None] = mapped_column(Integer)
    total: Mapped[int | None] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WikiAlbumCache(Base):
    """Parsed Wikipedia album data (infobox type, tracklist) keyed by QID —
    fetched once, then shared by every sync and scan run across restarts."""

    __tablename__ = "wiki_album_cache"

    qid: Mapped[str] = mapped_column(String(32), primary_key=True)
    record_type: Mapped[str | None] = mapped_column(String(16))
    tracklist: Mapped[list | None] = mapped_column(JSONB)
    # {"deezer": ..., "spotify": ..., "discogs": ...} from the entity claims;
    # {} when the entity carries none, NULL only for pre-column cache rows
    external_ids: Mapped[dict | None] = mapped_column(JSONB)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MusicFile(Base):
    """Every audio file in the library is a first-class row — the id is the
    identity, tags/name/quality are attributes; matching a file to a catalog
    track is a nullable link, never a precondition for existing here."""

    __tablename__ = "music_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    size: Mapped[int | None] = mapped_column(BigInteger)
    mtime: Mapped[float | None] = mapped_column(Float)
    tag_artist: Mapped[str | None] = mapped_column(String(500))
    tag_album: Mapped[str | None] = mapped_column(String(500))
    tag_title: Mapped[str | None] = mapped_column(String(500))
    tag_track: Mapped[int | None] = mapped_column(Integer)
    # Everything the file says, as it said it. Not what the record is called —
    # the catalogue answers that — but what THIS FILE carries, so a correction
    # can be seen and planned without opening forty-eight thousand of them, and
    # so the ids somebody else's tagger left behind are readable at all.
    # `mtime` and `size` above are what says it has gone stale.
    tags: Mapped[dict | None] = mapped_column(JSONB)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String(16))
    bitrate_kbps: Mapped[int | None] = mapped_column(Integer)
    sample_rate_hz: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    # the channel layout, which is not a resolution: a 5.1 or Atmos file is a
    # different MIX of the album, never a better copy of the stereo one. NULL
    # where the file was probed before this was read.
    channels: Mapped[int | None] = mapped_column(Integer)
    track_id: Mapped[int | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL"), index=True
    )
    # which download delivered this file — an album whose files carry more than
    # one is stitched from several rips, which is the thing we do not want.
    # NULL for files adopted from the existing library by a scan.
    # Deliberately NOT a foreign key: it is a record of where the file came
    # from, and clearing the download history must not be able to erase it —
    # a cascade to NULL made a stitched album read as a single clean rip.
    download_id: Mapped[int | None] = mapped_column(index=True)
    # when the house got it, which is what "recently added" means for music
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    track: Mapped["Track | None"] = relationship(back_populates="files")


class MusicDownload(Base):
    __tablename__ = "music_downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    release_id: Mapped[int] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"))
    channel: Mapped[ChannelName] = mapped_column(
        Enum(ChannelName, values_callable=lambda e: [m.value for m in e])
    )
    status: Mapped[MusicDownloadStatus] = mapped_column(
        Enum(MusicDownloadStatus, values_callable=lambda e: [m.value for m in e]),
        default=MusicDownloadStatus.QUEUED,
    )
    score: Mapped[float | None] = mapped_column()
    job_ref: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    release: Mapped[Release] = relationship(back_populates="downloads")


def _searched(column) -> Index:
    label = f"{column.key}_unaccented"
    return Index(f"ix_{column.table.name}_{label}", func.unaccented(column).label(label),
                 postgresql_using="gin", postgresql_ops={label: "gin_trgm_ops"})


# the trigram indexes the shelf search reads, made by the migration that made
# `unaccented`; named here so autogenerate does not offer to drop them
_searched(Artist.__table__.c.name)
_searched(Release.__table__.c.title)
_searched(Track.__table__.c.title)
