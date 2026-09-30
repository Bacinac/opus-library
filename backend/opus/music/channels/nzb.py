"""What a usenet post's file list means for a music library.

OPUS fetches the NZB and reports the names and sizes it declares; it never says
whether that is good enough, because "good enough" is a music library's question
and not a downloader's. This is the answer to that question — the half of the
old channel that stayed behind when the fetching moved out.

A post that is not archive-packed lists its real audio filenames, which turns the
title's promise into fact: the actual tracks to match against the catalog and the
actual extensions — the 'The Who-Tommy-Deluxe Edition-2CD-2003-BiM' post reads
like a lossless deluxe edition and holds 42 MP3s. A post that seals its tracks
inside RARs, or names every one of them alike, says nothing and stays judged by
its title as before."""

import posixpath
import re

from opus.music.channels.base import CandidateFile, Contents
from opus.music.tagging.tagger import AUDIO_EXTENSIONS

# the volumes a packed post is made of; par2 is repair data and rides along with
# both kinds, so it only speaks when there is no audio to speak for itself
_ARCHIVE = re.compile(r"\.(?:rar|r\d{2}|par2|zip|7z|tar|gz|\d{3})$", re.I)


def classify(declared: list[dict]) -> Contents:
    """Turn the file list OPUS reported into a verdict about the post."""
    files = [
        CandidateFile(name=f["name"], size=f.get("size", 0),
                      extension=posixpath.splitext(f["name"])[1].lower())
        for f in declared
    ]
    audio = [f for f in files if f.extension in AUDIO_EXTENSIONS]
    if audio:
        # a poster who gives every segment the same subject ('The.Who-Tommy
        # [The Who].flac' for all 24 tracks) hides them as thoroughly as a RAR
        if len({f.name for f in audio}) < len(audio):
            return Contents("obfuscated")
        return Contents("audio", audio)
    if any(_ARCHIVE.search(f.name) for f in files):
        return Contents("packed")
    return Contents("unknown")
