"""The /api aggregator.

Three halves under three prefixes, and above them the endpoints that belong to
the installation itself: settings, the login, the linked accounts and the one
download queue — which photographs stay out of, because a contribution is not an
acquisition. What a plugin adds sits beside them, under whatever prefix it
names.

Within each half the mounting order is the matcher's order. Music's artwork
router goes last on purpose — its /{entity}/{entity_id} prefix is a catch-all
that would swallow concrete paths registered after it."""

from fastapi import APIRouter
from opus_core import plugins, revision

from opus.api.routers import downloads, links, system
from opus.api.routers.music import artists, artwork, library, releases
from opus.api.routers.music import downloads as music_downloads
from opus.api.routers.photos import draw as photos_draw
from opus.api.routers.photos import library as photos_library
from opus.api.routers.photos import people as photos_people
from opus.api.routers.photos import sharing as photos_sharing
from opus.api.routers.photos import timeline as photos_timeline
from opus.api.routers.photos import vault as photos_vault
from opus.api.routers.video import discover, movies, series, videos
from opus.api.routers.video import downloads as video_downloads
from opus.api.routers.video import library as video_library
from opus.plugins import PLUGINS

router = APIRouter(prefix="/api")

router.include_router(system.router)
router.include_router(revision.router("opus-library"))
router.include_router(plugins.router(PLUGINS))
router.include_router(links.router)
router.include_router(downloads.router)
for path in (path for plugin in PLUGINS for path in plugin.routes):
    router.include_router(plugins.resolve(path))

music = APIRouter(prefix="/music")
music.include_router(artists.router)
music.include_router(releases.router)
music.include_router(library.router)
music.include_router(music_downloads.router)
music.include_router(artwork.router)
router.include_router(music)

photos = APIRouter(prefix="/photos")
photos.include_router(photos_library.router)
photos.include_router(photos_draw.router)
photos.include_router(photos_people.router)
photos.include_router(photos_vault.router)
photos.include_router(photos_sharing.router)
# last: its /{checksum} is a catch-all and would swallow everything above it
photos.include_router(photos_timeline.router)
router.include_router(photos)
router.include_router(photos_sharing.public)

video = APIRouter(prefix="/video")
video.include_router(discover.router)
video.include_router(movies.router)
video.include_router(series.router)
video.include_router(videos.router)
video.include_router(video_library.router)
video.include_router(video_downloads.router)
router.include_router(video)
