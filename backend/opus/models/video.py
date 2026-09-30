"""The video half of the library: films, series down to the episode, the web
videos pulled in by URL, the files that carry them and the subtitles that decide
whether a file counts as arrived.

The music half is in `opus.models.music`."""

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from opus.models.base import Base


class Movie(Base):
    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    imdb_id: Mapped[str | None] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    original_title: Mapped[str] = mapped_column(String, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str] = mapped_column(Text, default="")
    # the same text as TMDB has it in Croatian, when it has it. English stays
    # in overview and is what a reader falls back to
    overview_hr: Mapped[str] = mapped_column(Text, default="")
    poster_url: Mapped[str | None] = mapped_column(String)
    backdrop_url: Mapped[str | None] = mapped_column(String)
    runtime_min: Mapped[int | None] = mapped_column(Integer)
    genres: Mapped[list] = mapped_column(JSONB, default=list)
    studios: Mapped[list] = mapped_column(JSONB, default=list)
    directors: Mapped[list] = mapped_column(JSONB, default=list)
    # [{id, name, character, profile_url}] — the id is a TMDB person, so a face
    # on the details screen is a way into everything else they are in
    cast: Mapped[list] = mapped_column(JSONB, default=list)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    # subtitle policy override, e.g. "all:en,hr"; empty = inherit global
    subtitle_override: Mapped[str] = mapped_column(String, default="")
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    files: Mapped[list["VideoFile"]] = relationship(back_populates="movie")


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title: Mapped[str] = mapped_column(String)
    original_title: Mapped[str] = mapped_column(String, default="")
    year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str] = mapped_column(Text, default="")
    overview_hr: Mapped[str] = mapped_column(Text, default="")
    poster_url: Mapped[str | None] = mapped_column(String)
    backdrop_url: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="")  # Returning Series | Ended | ...
    genres: Mapped[list] = mapped_column(JSONB, default=list)
    studios: Mapped[list] = mapped_column(JSONB, default=list)
    directors: Mapped[list] = mapped_column(JSONB, default=list)
    cast: Mapped[list] = mapped_column(JSONB, default=list)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    subtitle_override: Mapped[str] = mapped_column(String, default="")
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    seasons: Mapped[list["Season"]] = relationship(back_populates="series", order_by="Season.number")


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("series_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("series.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    overview: Mapped[str] = mapped_column(Text, default="", server_default="")
    overview_hr: Mapped[str] = mapped_column(Text, default="", server_default="")
    # NULL is not asked yet; empty is asked, and TMDB has no poster for it
    poster_url: Mapped[str | None] = mapped_column(String)

    series: Mapped[Series] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(back_populates="season", order_by="Episode.number")


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("season_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id", ondelete="CASCADE"), index=True)
    tmdb_id: Mapped[int | None] = mapped_column(Integer)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String, default="")
    air_date: Mapped[datetime.date | None] = mapped_column(Date)
    overview: Mapped[str] = mapped_column(Text, default="")
    overview_hr: Mapped[str] = mapped_column(Text, default="")
    still_url: Mapped[str | None] = mapped_column(String)
    runtime_min: Mapped[int | None] = mapped_column(Integer)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)

    season: Mapped[Season] = relationship(back_populates="episodes")
    files: Mapped[list["VideoFile"]] = relationship(back_populates="episode")


class WebChannel(Base):
    """A followed web source (YouTube channel/playlist or anything yt-dlp
    understands); new uploads are picked up by the monitor loop."""

    __tablename__ = "web_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String, unique=True)
    external_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    thumb_url: Mapped[str | None] = mapped_column(String)
    monitored: Mapped[bool] = mapped_column(Boolean, default=True)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    videos: Mapped[list["WebVideo"]] = relationship(back_populates="channel")


class WebVideo(Base):
    __tablename__ = "web_videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("web_channels.id", ondelete="SET NULL"), index=True)
    external_id: Mapped[str] = mapped_column(String, unique=True)
    url: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String, default="")
    uploader: Mapped[str] = mapped_column(String, default="")
    thumb_url: Mapped[str | None] = mapped_column(String)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    published_at: Mapped[datetime.date | None] = mapped_column(Date)
    added_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    channel: Mapped[WebChannel | None] = relationship(back_populates="videos")
    files: Mapped[list["VideoFile"]] = relationship(back_populates="web_video")


class VideoFile(Base):
    """Files-first: every imported video file is a row with its own identity;
    links to a movie/episode/web video are attributes of the file."""

    __tablename__ = "video_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String, unique=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    container: Mapped[str] = mapped_column(String, default="")
    video_codec: Mapped[str] = mapped_column(String, default="")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    audio_langs: Mapped[list] = mapped_column(JSONB, default=list)
    duration_s: Mapped[float | None] = mapped_column(Float)
    # the post this file came down in, so a replacement never takes it again;
    # empty for a file the library found rather than fetched
    release_guid: Mapped[str] = mapped_column(String, default="", server_default="")
    release_title: Mapped[str] = mapped_column(String, default="", server_default="")
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id", ondelete="SET NULL"), index=True)
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id", ondelete="SET NULL"), index=True)
    web_video_id: Mapped[int | None] = mapped_column(ForeignKey("web_videos.id", ondelete="SET NULL"), index=True)
    scanned_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # where the closing credits begin. `credits_read_at` empty is a file nobody
    # has looked at yet; read and found nothing leaves credits_s empty with the
    # time it was looked at
    credits_s: Mapped[float | None] = mapped_column(Float)
    credits_read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # the opening titles, from where they begin to where the episode resumes,
    # read the same way and kept apart because a file read for its credits
    # before these existed has not been read for them
    intro_s: Mapped[float | None] = mapped_column(Float)
    intro_end_s: Mapped[float | None] = mapped_column(Float)
    intro_read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    movie: Mapped[Movie | None] = relationship(back_populates="files")
    episode: Mapped[Episode | None] = relationship(back_populates="files")
    web_video: Mapped[WebVideo | None] = relationship(back_populates="files")
    subtitles: Mapped[list["Subtitle"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="Subtitle.id")
    streams: Mapped[list["MediaStream"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="MediaStream.position")
    chapters: Mapped[list["Chapter"]] = relationship(
        back_populates="file", cascade="all, delete-orphan", order_by="Chapter.position")


class MediaStream(Base):
    """What is actually inside a video file: its picture and its sound, one row
    per stream, as ffprobe found them.

    The library already ran a full probe at import and kept only a de-duplicated
    list of languages — so a release with English TrueHD 7.1 and English stereo
    AC3 recorded one word, `en`, and anything wanting to choose between them had
    to open the file again. It is the library that owns what a file IS; nothing
    downstream should have to re-read an 89 GB remux to find out.

    Subtitles are deliberately NOT here. They live in `subtitles` because they
    are an acceptance criterion rather than a property of the container, and
    because half of them are sidecar files that are not streams in it at all."""

    __tablename__ = "media_streams"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("video_files.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # video | audio
    # the stream's number WITHIN its kind, which is what ffmpeg's -map 0:a:N
    # wants; the absolute index is of no use to anything downstream
    position: Mapped[int] = mapped_column(Integer)
    codec: Mapped[str] = mapped_column(String(32), default="")
    # what it is beyond the name of its core: "DTS-HD MA", "TrueHD + Atmos".
    # Both of those answer `dca` and `truehd` to a probe asked only for the
    # codec, and the mark on the screen would be the mark of the core.
    profile: Mapped[str | None] = mapped_column(String(48))
    lang: Mapped[str] = mapped_column(String(8), default="und")
    title: Mapped[str] = mapped_column(String, default="")
    channels: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    # how many pictures a second the file actually holds. A television can run at
    # the film's own rate and most can — but only if something tells them what it
    # is, and the container often does not say. Recorded here because the probe
    # that could answer it runs once, at import, over the whole file.
    frame_rate: Mapped[float | None] = mapped_column(Float)
    # how the picture's colour is encoded. Without this nothing downstream can
    # tell HDR from SDR, and an HDR picture re-encoded as if it were SDR comes
    # out the washed green and magenta that says "nobody tone-mapped this".
    color_transfer: Mapped[str] = mapped_column(String(24), default="")
    color_primaries: Mapped[str] = mapped_column(String(24), default="")
    default: Mapped[bool] = mapped_column(Boolean, default=False)
    forced: Mapped[bool] = mapped_column(Boolean, default=False)

    file: Mapped["VideoFile"] = relationship(back_populates="streams")


class Chapter(Base):
    """Where a film changes scene, as the disc that made it said.

    A remote skipping ten seconds at a time is how you look for something you
    can nearly remember; chapters are how you find it. Two thirds of the films
    here carry them and the probe that could read them already opens the file,
    so the only reason they were not recorded is that nobody asked."""

    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(
        ForeignKey("video_files.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    start_s: Mapped[float] = mapped_column(Float)
    # a disc often names them "Chapter 4", which is no better than the number
    title: Mapped[str] = mapped_column(String(200), default="")

    file: Mapped["VideoFile"] = relationship(back_populates="chapters")


class Subtitle(Base):
    __tablename__ = "subtitles"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("video_files.id", ondelete="CASCADE"), index=True)
    lang: Mapped[str] = mapped_column(String)  # ISO 639-1 where known, else "und"
    source: Mapped[str] = mapped_column(String)  # embedded | external | opensubtitles | youtube
    format: Mapped[str] = mapped_column(String, default="")  # srt | ass | subrip codec | ...
    forced: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    # which subtitle stream of the container this is, in ffmpeg's numbering —
    # what `-map 0:s:N` wants. NULL for a sidecar. It is kept on the row rather
    # than taken from the row's place in a list because Postgres returns rows in
    # the order the heap holds them, and one update is enough for that to stop
    # being the order they went in — after which every track is demuxed from
    # some other track's stream.
    stream_index: Mapped[int | None] = mapped_column(Integer)
    # sidecar file path; NULL for streams embedded in the container
    path: Mapped[str | None] = mapped_column(String)
    # where a browser-ready copy of this track lives. An embedded subtitle has
    # to be read out of the container before anything can show it, and pulling
    # one out of an 89 GB remux takes three minutes — which is three minutes
    # after the player has given up. It is done once, here, and this is where
    # the result is.
    vtt_path: Mapped[str | None] = mapped_column(String)
    # read out of the container and found without a single cue: a styles-only
    # .ass header is a track ffprobe lists and nothing can show. Without this
    # mark it looks unread, and every start read 69 whole files to learn
    # again that there is nothing in them.
    hollow: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    file: Mapped[VideoFile] = relationship(back_populates="subtitles")


class DeadPost(Base):
    """A post that could not be completed, by the indexer's guid. Kept apart from
    the queue row that found it out, so clearing the queue does not bring the
    post back into the running."""
    __tablename__ = "dead_posts"

    guid: Mapped[str] = mapped_column(String, primary_key=True)
    release_title: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    died_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VideoDownload(Base):
    __tablename__ = "video_downloads"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String)  # movie | episode | web_video
    movie_id: Mapped[int | None] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    web_video_id: Mapped[int | None] = mapped_column(ForeignKey("web_videos.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String)  # sabnzbd | qbittorrent | ytdlp
    release_title: Mapped[str] = mapped_column(String, default="")
    release_guid: Mapped[str] = mapped_column(String, default="", server_default="")
    job_ref: Mapped[dict] = mapped_column(JSONB, default=dict)
    # queued | downloading | downloaded | imported | waiting_subtitles | failed
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # one queue lists both halves, and a row there says what was WANTED rather
    # than which release happened to carry it
    movie: Mapped[Movie | None] = relationship()
    episode: Mapped[Episode | None] = relationship()
    web_video: Mapped[WebVideo | None] = relationship()


class AwardTitle(Base):
    """A title as an award shelf shows it. Kept apart from the wins because one
    film takes the Oscar and the BAFTA, and one series the Emmy three times."""

    __tablename__ = "award_titles"
    __table_args__ = (UniqueConstraint("media_type", "tmdb_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[str] = mapped_column(String)  # movie | tv
    tmdb_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String)
    year: Mapped[int | None] = mapped_column(Integer)
    poster_url: Mapped[str | None] = mapped_column(String)
    vote_average: Mapped[float] = mapped_column(Float, default=0.0)


class Streaming(Base):
    """Where a title is on a subscription service, as JustWatch last said."""

    __tablename__ = "streaming"
    __table_args__ = (UniqueConstraint("media_type", "tmdb_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[str] = mapped_column(String)  # movie | tv
    tmdb_id: Mapped[int] = mapped_column(Integer)
    services: Mapped[list] = mapped_column(JSONB, default=list)
    checked_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)
    asked_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), index=True)


class AwardWin(Base):
    __tablename__ = "award_wins"
    __table_args__ = (UniqueConstraint("award", "year", "title_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    award: Mapped[str] = mapped_column(String, index=True)
    year: Mapped[int] = mapped_column(Integer)
    title_id: Mapped[int] = mapped_column(ForeignKey("award_titles.id", ondelete="CASCADE"), index=True)
    refreshed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))

    title: Mapped[AwardTitle] = relationship()
