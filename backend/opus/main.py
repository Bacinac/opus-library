import asyncio
import logging
from contextlib import asynccontextmanager

import opus_auth
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opus_core import dida
from opus_core.door import Door
from opus_core.encoding import CompressJSON

from opus import acquire, auth, landing
from opus.api.routes import router
from opus.db import SessionLocal
from opus.music.pipeline import (download as music_download, monitor as music_monitor,
                                 records as music_records)
from opus.photos import pipeline as photos_pipeline
from opus.settings_store import current_runtime
from opus.video import awards as video_awards
from opus.video import credits as video_credits
from opus.video import library_scan
from opus.video.pipeline import (download as video_download, monitor as video_monitor,
                                 profiles as video_profiles)
from opus.video import streaming as video_streaming
from opus.video.metadata import tmdb

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# libvips narrates every operation it performs — the mask width, the vector
# path, each threadpool that finished — and pyvips forwards all of it to Python
# at INFO. Deriving a photograph produces about forty such lines, so a pass over
# the library writes several million: 98 % of the backend log, which is the same
# as having no backend log. Its warnings still come through.
logging.getLogger("pyvips").setLevel(logging.WARNING)

# httpx narrates every request with its whole address, and some of the services
# this talks to take their key in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


opus_auth.quiet_access_log()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # each half watches its own downloads and its own followed things: an
    # episode airing and a record coming out are the same event to the user and
    # nothing alike to the code that has to notice them
    loops = [
        asyncio.create_task(music_download.poll_downloads_loop()),
        asyncio.create_task(music_monitor.monitor_loop()),
        asyncio.create_task(music_records.records_loop()),
        asyncio.create_task(video_download.poll_downloads_loop()),
        asyncio.create_task(video_monitor.monitor_loop()),
        asyncio.create_task(video_profiles.profiles_loop()),
        asyncio.create_task(video_awards.awards_loop()),
        asyncio.create_task(video_streaming.streaming_loop()),
        asyncio.create_task(video_credits.credits_loop()),
        asyncio.create_task(landing.sweep_loop()),
        asyncio.create_task(library_scan.resume_extract()),
        # the photograph half had no loop of its own: every pass waited
        # to be asked, so a folder dropped in stayed a folder of files
        asyncio.create_task(photos_pipeline.watch_loop()),
    ]
    yield
    for loop in loops:
        loop.cancel()
    await asyncio.gather(*loops, return_exceptions=True)
    await acquire.close()
    await tmdb.close()
    await dida.close()


class _Library(FastAPI):
    def build_middleware_stack(self):
        return CompressJSON(opus_auth.secured(super().build_middleware_stack()))


app = _Library(title="OPUS · Library", lifespan=lifespan,
               docs_url=None, redoc_url=None, openapi_url=None)


async def refusal(request: Request) -> JSONResponse | None:
    """One door for every route, so a route cannot be added and forgotten."""
    async with SessionLocal() as session:
        config = await current_runtime()
        roster = await auth.roster(session)
    path = request.url.path
    cookie = request.cookies.get(opus_auth.SESSION_COOKIE)
    token = request.headers.get(opus_auth.TOKEN_HEADER)
    module = auth.consumer(config, token) is not None
    # a cookie on the parent domain is written and cleared by the open paths too,
    # and a page elsewhere reaches those without carrying one
    if (cookie or path in auth.OPEN_PATHS) and not module and not opus_auth.same_origin(request):
        return JSONResponse({"detail": "not sent from this module's pages"}, status_code=403)
    if not auth.allowed(config, roster, path, cookie, token, request.method):
        # signed in and not allowed is a different answer from not signed in,
        # and the browser acts on the difference: one sends you to the login
        # screen you already passed, the other tells you the truth
        if module or auth.whoami(roster, cookie):
            return JSONResponse({"detail": "not yours to change"}, status_code=403)
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    return None


app.add_middleware(Door, refusal=refusal)


app.include_router(router)
