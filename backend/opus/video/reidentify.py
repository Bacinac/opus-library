"""Films adopted under the wrong name, put under the right one.

Every film a scan adopted is identified again from its folder and file name
together, the way the scan identifies one now. A film that turns out to be a
different one keeps its row and is pointed at the right TMDB id, with its details
fetched again: the row's id is what the player's progress and what has been
watched hang off, and a new row would leave both behind. A film that came in
through a download was asked for by its TMDB id and is not second-guessed, and
neither is one whose name and year fit it exactly as well as another film.

    docker compose exec backend python -m opus.video.reidentify
    docker compose exec backend python -m opus.video.reidentify --write
"""

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from opus.db import SessionLocal
from opus.models import Movie, VideoDownload
from opus.settings_store import current_runtime
from opus.video.library_scan import movie_candidates, movie_guess
from opus.video.metadata import tmdb


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true",
                        help="point the films at the right TMDB id (default: only report)")
    args = parser.parse_args()

    async with SessionLocal() as session:
        config = await current_runtime()
        root = Path(config.get("movies_dir"))
        movies = list((await session.execute(
            select(Movie).options(selectinload(Movie.files)).order_by(Movie.id))).scalars())
        downloaded = set((await session.execute(
            select(VideoDownload.movie_id).where(VideoDownload.state == "imported",
                                                 VideoDownload.movie_id.is_not(None)))).scalars())
        held = {m.tmdb_id: m for m in movies}
        wrong = 0
        for movie in movies:
            if not movie.files or movie.id in downloaded:
                continue
            path = Path(movie.files[0].path)
            found = await movie_candidates(config, movie_guess(path, root))
            if not found or any(f["tmdb_id"] == movie.tmdb_id for f in found):
                continue
            match = found[0]
            wrong += 1
            print(f"#{movie.id} {movie.title} ({movie.year}) -> "
                  f"{match['title']} ({match.get('year')}) tmdb {match['tmdb_id']}  [{path}]")
            if match["tmdb_id"] in held:
                print(f"    left alone: #{held[match['tmdb_id']].id} is already that film")
                continue
            if args.write:
                del held[movie.tmdb_id]
                for key, value in (await tmdb.movie_details(config, match["tmdb_id"])).items():
                    setattr(movie, key, value)
                held[movie.tmdb_id] = movie
        if args.write:
            await session.commit()
        print(f"{wrong} adopted films named wrongly" + ("" if args.write else " (report only)"))


if __name__ == "__main__":
    asyncio.run(main())
