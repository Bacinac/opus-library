"""Grouping the faces, still without naming anybody.

Agglomerative clustering is the obvious method and it is impossible here: it
wants every pairwise distance, and 67,492 faces is a matrix of 18 GB — more than
this machine has. So the shape of the problem is turned around. Each face asks
the HNSW index for its nearest few, which is the one operation that index exists
to make cheap, and the answers become the edges of a graph whose connected
components are the clusters.

Two rules keep that graph from collapsing into one blob. An edge needs the
similarity to clear a threshold, and it needs to be **mutual** — A among B's
nearest and B among A's. Without mutuality a single ambiguous face acts as a
bridge and chains two people into one cluster; with it, a face has to be
somebody's close neighbour as well as having them as its own.

Nothing here writes a name. A cluster is a likeness the machine found, and it
carries a pointer to a person only once a human puts one there — which is why
running this again costs nothing: the faces keep their own person_id, and a
fresh cluster is handed whoever its faces already belong to.
"""

import asyncio
import logging
import zlib
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text

from opus.db import SessionLocal
from opus.models import FACE_GENERATION, Face, FaceCluster
from opus.photos.people import recognise
from opus.settings_store import current_runtime

log = logging.getLogger(__name__)


class _Union:
    """Union-find that refuses a merge which would put two faces from one
    photograph into the same cluster.

    Each component carries the set of photographs it draws from. A merge is
    allowed only when those sets are disjoint — so a face that resembles two
    people who were photographed together can join at most one of them, and the
    chain that welds them stops at the first picture they share."""

    def __init__(self, ids: list[int], photo_of: dict[int, int]):
        self.parent = {i: i for i in ids}
        self.photos = {i: {photo_of[i]} for i in ids}

    def find(self, a: int) -> int:
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        pa, pb = self.photos[ra], self.photos[rb]
        # checked against the smaller set, and the smaller is merged into the
        # larger, so the whole pass stays close to linear
        if len(pa) > len(pb):
            ra, rb, pa, pb = rb, ra, pb, pa
        if not pa.isdisjoint(pb):
            return False
        self.parent[ra] = rb
        pb |= pa
        self.photos.pop(ra, None)
        return True


class Regrouping(Exception):
    """The faces are being regrouped, so a change to who is in a group, made now,
    would be written over when the regrouping commits."""


# A regrouping holds it whole while it rewrites the groups; every route that
# names, moves or throws away faces holds it shared for its own transaction.
REGROUPING = zlib.crc32(b"opus.photos.regrouping")


async def unmoved(session) -> None:
    """Raise Regrouping unless no regrouping is rewriting the groups — and keep
    one from starting until this transaction ends."""
    if not await session.scalar(text("SELECT pg_try_advisory_xact_lock_shared(:key)"),
                                {"key": REGROUPING}):
        raise Regrouping()


@dataclass(frozen=True)
class _Cuts:
    threshold: float
    min_core: int
    adopt: float
    window_years: int
    neighbours: int
    weld: float
    weld_shared: float
    weld_agree: float
    sharpness: float


def _cuts(config) -> _Cuts:
    return _Cuts(
        threshold=config.float("faces_threshold"),
        min_core=int(config.float("faces_min_core")),
        adopt=config.float("faces_adopt"),
        window_years=max(1, int(config.float("faces_window_years"))),
        neighbours=int(config.float("faces_neighbours")),
        weld=config.float("faces_weld"),
        weld_shared=config.float("faces_weld_shared"),
        weld_agree=config.float("faces_weld_agree"),
        sharpness=config.float("faces_min_sharpness"),
    )


async def run() -> dict:
    """Regroup every face and offer the new groups to the people already known,
    as one transaction. Cut off anywhere, it leaves the previous grouping whole
    and the new faces still waiting, which is what starts it again."""
    async with SessionLocal() as session:
        cuts = _cuts(await current_runtime())
        ids, photo_of, window = await _faces(session, cuts)
        near, strength = await _neighbours(session, cuts, ids, window)
        groups = await asyncio.to_thread(_group, ids, photo_of, near, strength, cuts.min_core)
        # taken only now, so a name given while the neighbours were being asked
        # is read below and carried into the new groups
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": REGROUPING})
        kept = await _rebuild(session, cuts, groups)
        adopted = await _adopt_lone(session, cuts)
        # a face adopted elsewhere leaves its own group behind it, holding
        # nothing. Ten thousand empty groups is not a bug anyone sees, and
        # every count taken from the table rather than from the faces is
        # wrong for as long as they sit there
        await drop_empty(session)
        joined = await _weld_islands(session, cuts)
        # the centroids belong to the grouping and are written with it, so
        # the run view never has to work them out
        await refresh_centroids(session)
        offered = await recognise.run(session)
        clusters = (await session.execute(
            select(func.count()).select_from(FaceCluster)
            .where(FaceCluster.generation == FACE_GENERATION))).scalar_one()
        await session.commit()
    return {"clusters": clusters, "kept_names": kept, "adopted": adopted, "welded": joined,
            "named": offered["named"]}


async def _faces(session, cuts: _Cuts) -> tuple[list[int], dict[int, int], dict[int, int | None]]:
    # A face that was never measured still takes part: the measurement
    # is newer than the faces, and refusing what has not been asked would
    # empty the library rather than clean it.
    rows = (await session.execute(text("""
        SELECT f.id, EXTRACT(YEAR FROM p.taken_at)::int, f.photo_id
        FROM photo_faces f JOIN photos p ON p.id = f.photo_id
        WHERE f.generation = :gen
          AND (f.sharpness IS NULL OR f.sharpness >= :sharp)
        ORDER BY f.id
    """), {"gen": FACE_GENERATION, "sharp": cuts.sharpness})).all()
    ids = [r[0] for r in rows]
    photo_of = {r[0]: r[2] for r in rows}
    # a face on an undated photograph goes in a window of its own rather
    # than into everyone else's: no date is not a date in the middle
    window = {fid: (year // cuts.window_years if year is not None else None)
              for fid, year, _ in rows}
    return ids, photo_of, window


async def _neighbours(session, cuts: _Cuts, ids: list[int], window: dict[int, int | None]
                      ) -> tuple[dict[int, set[int]], dict[tuple[int, int], float]]:
    # Asked of the index one face at a time. The alternative — a single
    # self-join over 67,000 rows — is the pairwise matrix again under
    # another name, and the index cannot help with it.
    near: dict[int, set[int]] = {}
    strength: dict[tuple[int, int], float] = {}
    step = 500
    for start in range(0, len(ids), step):
        rows = (await session.execute(text("""
            SELECT f.id AS src, n.id AS dst, (1 - n.d) AS sim
            FROM photo_faces f
            CROSS JOIN LATERAL (
                SELECT o.id, o.embedding <=> f.embedding AS d
                FROM photo_faces o
                WHERE o.generation = :gen AND o.id <> f.id
                ORDER BY o.embedding <=> f.embedding
                LIMIT :k
            ) n
            WHERE f.id = ANY(:ids) AND (1 - n.d) >= :thr
        """), {"gen": FACE_GENERATION, "k": cuts.neighbours,
               "ids": ids[start:start + step], "thr": cuts.threshold})).all()
        for src, dst, sim in rows:
            # the whole point: a resemblance across a decade is not
            # evidence, it is the sister problem wearing a high score
            if window.get(src) is not None and window.get(src) == window.get(dst):
                near.setdefault(src, set()).add(dst)
                strength[(min(src, dst), max(src, dst))] = float(sim)
    return near, strength


def _group(ids: list[int], photo_of: dict[int, int], near: dict[int, set[int]],
           strength: dict[tuple[int, int], float], min_core: int) -> list[list[int]]:
    union = _Union(ids, photo_of)
    # A face is a core if enough others are genuinely close to it. Only
    # cores may merge groups; everyone else is carried along by the core
    # they are nearest to, and carries nothing themselves.
    mutual = {a: {b for b in friends if a in near.get(b, ())}
              for a, friends in near.items()}
    core = {a for a, m in mutual.items() if len(m) >= min_core}
    # Strongest first. Once a merge can be refused — and it can, when
    # the two sides share a photograph — the order stops being cosmetic:
    # a weak link must not take a place a strong one had earned.
    pairs = sorted(
        ((strength.get((min(a, b), max(a, b)), 0.0), a, b)
         for a in core for b in mutual[a] if b in core and a < b),
        reverse=True)
    for _, a, b in pairs:
        union.union(a, b)
    # the rest attach to their nearest core without joining anything
    for a, m in mutual.items():
        if a in core:
            continue
        for b in sorted(m, key=lambda x: -strength.get((min(a, x), max(a, x)), 0.0)):
            if b in core and union.union(b, a):
                break

    groups: dict[int, list[int]] = {}
    for i in ids:
        groups.setdefault(union.find(i), []).append(i)
    return list(groups.values())


async def _rebuild(session, cuts: _Cuts, groups: list[list[int]]) -> int:
    """Replace every group with the new ones, and say how many of them kept a
    name."""
    # A face that a human has already assigned keeps that assignment; the
    # new cluster simply inherits whoever most of its faces belong to.
    # This is what makes re-clustering free rather than destructive.
    assigned = dict((await session.execute(
        select(Face.id, Face.person_id)
        .where(Face.generation == FACE_GENERATION,
               Face.person_id.isnot(None)))).all())

    # A face left out of this pass must let go of the group it was in
    # last time, or it keeps pointing at a group that no longer exists
    await session.execute(
        Face.__table__.update()
        .where(Face.generation == FACE_GENERATION,
               Face.sharpness < cuts.sharpness)
        .values(cluster_id=None))
    await session.execute(delete(FaceCluster).where(
        FaceCluster.generation == FACE_GENERATION))
    await session.flush()

    kept = 0
    for members in groups:
        votes: dict[int, int] = {}
        for m in members:
            p = assigned.get(m)
            if p is not None:
                votes[p] = votes.get(p, 0) + 1
        person = max(votes, key=votes.get) if votes else None
        if person is not None:
            kept += 1
        cluster = FaceCluster(person_id=person, generation=FACE_GENERATION)
        session.add(cluster)
        await session.flush()
        await session.execute(
            Face.__table__.update()
            .where(Face.id.in_(members))
            .values(cluster_id=cluster.id))

    await session.flush()
    return kept


async def _adopt_lone(session, cuts: _Cuts) -> int:
    """The lone faces, offered to the group they are nearest.

    Before consolidation, not after. A lone face joining a group cannot
    chain two people together — it connects nothing to nothing — so it
    is safe to do first, and doing it first is worth a third of the
    remaining fragmentation: the islands go into the consolidation
    already holding the faces that belong to them, so their centroids
    are of a person rather than of a pose, and there are more links
    between them for the agreement rule to weigh."""
    adopted = 0
    alone = (await session.execute(text("""
        SELECT f.id FROM photo_faces f
        WHERE f.generation = :gen AND f.cluster_id IN (
            SELECT cluster_id FROM photo_faces
            WHERE generation = :gen AND cluster_id IS NOT NULL
            GROUP BY cluster_id HAVING count(*) = 1)
        ORDER BY f.id
    """), {"gen": FACE_GENERATION})).scalars().all()
    for start in range(0, len(alone), 500):
        batch = alone[start:start + 500]
        # Into its own year and no other. Without this a single stray
        # face carries a group's span from two years to twenty, and the
        # exact date of the photograph — the one thing that is not an
        # estimate — stops being a property of the group at all.
        #
        # The nearest few are asked of the index and the year filtered
        # afterwards, rather than filtered inside: a condition under the
        # ORDER BY is applied after the index has already chosen, and the
        # answer comes back empty rather than merely different.
        found = (await session.execute(text("""
            SELECT DISTINCT ON (f.id) f.id, n.cluster_id, (1 - n.d) AS sim
            FROM photo_faces f
            JOIN photos pf ON pf.id = f.photo_id
            CROSS JOIN LATERAL (
                SELECT o.cluster_id, o.photo_id, o.embedding <=> f.embedding AS d
                FROM photo_faces o
                WHERE o.generation = :gen AND o.cluster_id IS NOT NULL
                  AND o.cluster_id <> f.cluster_id
                ORDER BY o.embedding <=> f.embedding
                LIMIT :k
            ) n
            JOIN photos po ON po.id = n.photo_id
            WHERE f.id = ANY(:ids) AND (1 - n.d) >= :thr
              AND pf.taken_at IS NOT NULL AND po.taken_at IS NOT NULL
              AND EXTRACT(YEAR FROM po.taken_at)::int / :wy
                = EXTRACT(YEAR FROM pf.taken_at)::int / :wy
            ORDER BY f.id, n.d
        """), {"gen": FACE_GENERATION, "ids": batch, "thr": cuts.adopt,
               "k": cuts.neighbours, "wy": cuts.window_years})).all()
        for fid, cid, _ in found:
            # never into a group that already holds this photograph:
            # the same rule as before, and for the same reason
            clash = (await session.execute(text("""
                SELECT 1 FROM photo_faces a
                WHERE a.cluster_id = :cid
                  AND a.photo_id = (SELECT photo_id FROM photo_faces WHERE id = :fid)
                LIMIT 1
            """), {"cid": cid, "fid": fid})).first()
            if clash:
                continue
            await session.execute(
                Face.__table__.update().where(Face.id == fid).values(cluster_id=cid))
            adopted += 1
    return adopted


async def _weld_islands(session, cuts: _Cuts) -> int:
    """The islands within one year, made into one group."""
    pairs = (await session.execute(text("""
        WITH c AS (
            SELECT f.cluster_id AS id, AVG(f.embedding::vector) AS v,
                   min(EXTRACT(YEAR FROM p.taken_at)::int) / :wy AS win,
                   count(DISTINCT f.photo_id) AS photos
            FROM photo_faces f JOIN photos p ON p.id = f.photo_id
            WHERE f.generation = :gen AND f.cluster_id IS NOT NULL
              AND p.taken_at IS NOT NULL
            GROUP BY f.cluster_id
        )
        SELECT a.id, b.id, (1 - (a.v <=> b.v)) AS sim
        FROM c a JOIN c b ON b.win = a.win AND b.id > a.id
        WHERE (1 - (a.v <=> b.v)) >= :weld
          AND (SELECT count(DISTINCT x.photo_id) FROM photo_faces x
                WHERE x.cluster_id = a.id AND x.photo_id IN
                  (SELECT y.photo_id FROM photo_faces y
                    WHERE y.cluster_id = b.id))
              <= greatest(1, :share * least(a.photos, b.photos))
        ORDER BY sim DESC
    """), {"gen": FACE_GENERATION, "wy": cuts.window_years, "weld": cuts.weld,
           "share": cuts.weld_shared})).all()

    survivors, joined = await asyncio.to_thread(
        _welds, [(a, b, float(sim)) for a, b, sim in pairs], cuts.weld, cuts.weld_agree)
    if survivors:
        touched = set(survivors) | {g for gone in survivors.values() for g in gone}
        owners = dict((await session.execute(
            select(FaceCluster.id, FaceCluster.person_id)
            .where(FaceCluster.id.in_(list(touched))))).all())
        for keep, gone in survivors.items():
            await session.execute(
                Face.__table__.update()
                .where(Face.cluster_id.in_(gone))
                .values(cluster_id=keep))
            if owners.get(keep) is None:
                named = next((owners[g] for g in gone if owners.get(g)), None)
                if named is not None:
                    await session.execute(
                        FaceCluster.__table__.update()
                        .where(FaceCluster.id == keep)
                        .values(person_id=named))
            await session.execute(
                delete(FaceCluster).where(FaceCluster.id.in_(gone)))
    log.info("welding: %d groups joined from %d candidate links",
             joined, len(pairs))
    return joined


def _welds(pairs: list[tuple[int, int, float]], weld: float,
           weld_agree: float) -> tuple[dict[int, list[int]], int]:
    """Which groups each surviving group swallows, strongest link first, and how
    many joins that took."""
    link = {(a, b): sim for a, b, sim in pairs}
    held: dict[int, int] = {}
    mem: dict[int, list[int]] = {}

    def root(x: int) -> int:
        held.setdefault(x, x)
        while held[x] != x:
            held[x] = held[held[x]]
            x = held[x]
        return x

    joined = 0
    for a, b, _sim in pairs:
        ra, rb = root(a), root(b)
        if ra == rb:
            continue
        left = mem.setdefault(ra, [ra])
        right = mem.setdefault(rb, [rb])
        agree = sum(1 for x in left for y in right
                    if link.get((min(x, y), max(x, y)), 0.0) >= weld)
        if agree < weld_agree * len(left) * len(right):
            continue
        held[rb] = ra
        left.extend(right)
        mem.pop(rb, None)
        joined += 1

    survivors: dict[int, list[int]] = {}
    for cid in list(held):
        r = root(cid)
        if r != cid:
            survivors.setdefault(r, []).append(cid)
    return survivors, joined


async def refresh_centroids(session, ids: list[int] | None = None) -> None:
    """Each group's centroid and size, from the faces it holds now: all of them,
    or the ones given — a face moved out of a group moves its middle."""
    await session.execute(text("""
        UPDATE photo_face_clusters c
        SET centroid = fresh.v::halfvec, faces_at = fresh.n
        FROM (SELECT f.cluster_id AS id, count(*) AS n,
                     AVG(f.embedding::vector) AS v
              FROM photo_faces f
              WHERE f.generation = :gen AND f.cluster_id IS NOT NULL
                AND (CAST(:ids AS integer[]) IS NULL OR f.cluster_id = ANY(:ids))
              GROUP BY f.cluster_id) fresh
        WHERE fresh.id = c.id
    """), {"gen": FACE_GENERATION, "ids": ids})


async def drop_empty(session) -> None:
    """A group whose faces have all left is not a group any more."""
    await session.execute(text("""
        DELETE FROM photo_face_clusters c
        WHERE NOT EXISTS (SELECT 1 FROM photo_faces f WHERE f.cluster_id = c.id)
    """))
