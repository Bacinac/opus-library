"""What is at a web address — the catalogue half of web video.

The downloading moved to OPUS; this did not. Enumerating a channel's uploads or
reading a URL's metadata is the same kind of knowledge as looking an album up in
a store's catalogue: it decides WHAT to want, which is the library's question. OPUS
answers the other one, how to fetch it.

yt-dlp stays a dependency here for exactly this reason, and for nothing else."""

import asyncio

import yt_dlp

from opus.acquire import AcquireError


async def probe_url(url: str) -> dict:
    """Metadata-only fetch (no download). Returns the yt-dlp info dict; for
    channels/playlists entries are flat (id/title/url only)."""
    def _probe():
        opts = {"quiet": True, "noprogress": True, "extract_flat": "in_playlist",
                "playlistend": 50}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    try:
        return await asyncio.to_thread(_probe)
    except Exception as exc:
        raise AcquireError(f"yt-dlp could not read {url}: {exc}") from exc
