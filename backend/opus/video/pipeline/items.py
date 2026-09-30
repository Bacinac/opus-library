"""The films and episodes the pipeline works for: which are wanted, and which
are already on their way."""

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus.models import VideoDownload, Episode, Season


async def episode_with_series(session, episode_id: int) -> Episode:
    result = await session.execute(
        select(Episode).where(Episode.id == episode_id)
        .options(selectinload(Episode.season).selectinload(Season.series)))
    return result.scalar_one()


ACTIVE_STATES = ("queued", "downloading", "downloaded")


async def active_ids(session, column) -> set:
    result = await session.execute(
        select(column).where(VideoDownload.state.in_(ACTIVE_STATES), column.is_not(None)))
    return {row[0] for row in result}


async def coming(session, *, movie_id: int | None = None,
                 episode_id: int | None = None) -> bool:
    """Something is already on its way for this one. Asking again does not make
    it arrive sooner — it makes two of it arrive, off two channels, into the
    same folder. The monitor pass has always checked; whoever asks by hand did
    not, so pressing a line twice grabbed it twice."""
    if movie_id is not None:
        return movie_id in await active_ids(session, VideoDownload.movie_id)
    if episode_id is not None:
        return episode_id in await active_ids(session, VideoDownload.episode_id)
    return False


def wanted(episode: Episode) -> bool:
    """Whether anything is looking for this episode.

    The season and the episode decide it. Holding a series is how a new season
    gets noticed at all; it is not a veto over the seasons already asked for,
    and reading it as one is what left an episode of a monitored season
    unclaimed for sixteen days — the season said yes, the episode said yes, and
    the pass never looked at either because the series said nothing.

    The word an episode wears on screen is this same question, asked once here
    so that what is shown and what is chased cannot drift apart."""
    return episode.monitored and episode.season.monitored
