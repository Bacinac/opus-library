"""One schema, several vocabularies.

Everything is re-exported here so a caller writes `from opus.models import
Artist` or `from opus.models import Movie` without having to know which half it
is asking for — while the halves themselves stay in separate files, because
nothing in one has any business referring to the other."""

from opus.models.accounts import Device, Passkey, User
from opus.models.base import Base, LoopPass, Setting
from opus.models.music import (
    ARTIST_CATALOG_IDS,
    DEAD_SOURCE,
    DOWNLOAD_SETTLED,
    DOWNLOAD_SPENT,
    RELEASE_SOURCE_IDS,
    TRACK_CATALOG_IDS,
    Artist,
    ArtistExternalId,
    ArtistRelation,
    ChannelName,
    DiscogsMastersCache,
    EnrichStatus,
    FolderScanCache,
    Image,
    MusicDownload,
    MusicDownloadStatus,
    MusicFile,
    Release,
    ReleaseStatus,
    ReleaseVariant,
    TidalAccount,
    Track,
    TrackLyrics,
    WikiAlbumCache,
)
from opus.models.people import (
    FACE_DIMS,
    COMPARED,
    FACE_GENERATION,
    Face,
    FaceCluster,
    Person,
)
from opus.models.photos import (
    FILE_MISSING,
    FILE_PRESENT,
    FILE_QUARANTINED,
    TIME_EXIF,
    TIME_FILENAME,
    TIME_FILE_MTIME,
    TIME_NONE,
    TIME_PATH,
    TIME_PEOPLE,
    Photo,
    PhotoFile,
    PhotoOffer,
    PhotoIntegrityEvent,
    PhotoShare,
    PhotoShareItem,
)
from opus.models.vault import VaultFile, VaultKey
from opus.models.video import (
    AwardTitle,
    AwardWin,
    Episode,
    Chapter,
    MediaStream,
    Movie,
    Season,
    Series,
    Streaming,
    Subtitle,
    DeadPost,
    VideoDownload,
    VideoFile,
    WebChannel,
    WebVideo,
)

__all__ = [
    "COMPARED",
    "FACE_DIMS",
    "FACE_GENERATION",
    "Face",
    "FaceCluster",
    "Person",

    "Base", "LoopPass", "Setting",
    # music
    "Artist", "ArtistExternalId", "ArtistRelation", "ChannelName",
    "DiscogsMastersCache", "EnrichStatus", "FolderScanCache", "Image",
    "MusicDownload", "MusicDownloadStatus", "MusicFile", "Release",
    "ReleaseStatus", "ReleaseVariant", "TidalAccount", "Track", "TrackLyrics",
    "WikiAlbumCache", "DOWNLOAD_SETTLED", "DOWNLOAD_SPENT", "DEAD_SOURCE",
    "ARTIST_CATALOG_IDS", "RELEASE_SOURCE_IDS", "TRACK_CATALOG_IDS",
    # photos
    "Photo", "PhotoFile", "PhotoOffer", "PhotoIntegrityEvent", "PhotoShare", "PhotoShareItem",
    # the household
    "Device",
    "Passkey",
    "User",
    "FILE_PRESENT", "FILE_MISSING", "FILE_QUARANTINED",
    "TIME_EXIF", "TIME_FILENAME", "TIME_PATH", "TIME_PEOPLE", "TIME_FILE_MTIME",
    "TIME_NONE",
    # video
    "AwardTitle", "AwardWin", "Chapter", "Episode", "MediaStream", "Movie", "Season", "Series", "Streaming",
    "DeadPost", "Subtitle", "VideoDownload", "VideoFile", "WebChannel", "WebVideo",
    # the private half of the photographs
    "VaultFile", "VaultKey",
]
