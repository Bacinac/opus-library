"""Giving a new face the name it already has.

The grouping works inside a year, which is what keeps two sisters apart — and
which means a year that has just arrived is a set of groups nobody has named.
Nothing in the grouping can fix that: it is built to refuse exactly the
comparison that would be needed, because comparing a face to another year is how
one child becomes her sister.

So it is asked afterwards, and asked narrowly. A new group is offered to the
people who were photographed around the same time — a person's face two years
either side of this one, not their face at any age — and it takes the name only
if one of them is clearly nearer than the rest and the calendar allows it.

The cut is the one measured for joining groups across years: over this library's
own faces, two groups above 0.62 were the same person every time, while two
different people average 0.028 and reach 0.43 once in a hundred.
"""

from sqlalchemy import text

from opus.models import COMPARED

# How alike a new group must be to somebody already named.
KNOWN = 0.62

# How much nearer than the next candidate. A face that suits two people equally
# is not evidence about either of them, and a name put on it wrongly spreads:
# the next pass sees a named group and offers it as a match in its turn.
CLEARLY = 0.06

# How far either side to look for the same person. Faces change; a face two
# years away is still recognisably the same one, and a face ten years away is
# the comparison the whole design exists to refuse.
WITHIN_YEARS = 3


async def run(session) -> dict:
    """Offer every nameless group to the people already known.

    Returns what it did rather than logging it, so the pass that calls this as
    one step of a longer chain can say what happened."""
    named, unsure = 0, 0
    rows = (await session.execute(text("""
        WITH nameless AS (
            SELECT c.id, c.centroid,
                   min(EXTRACT(YEAR FROM p.taken_at))::int AS year,
                   min(p.taken_at)::date AS first
            FROM photo_face_clusters c
            JOIN photo_faces f ON f.cluster_id = c.id
            JOIN photos p ON p.id = f.photo_id
            WHERE c.person_id IS NULL AND c.centroid IS NOT NULL
              AND c.faces_at >= :compared AND p.taken_at IS NOT NULL
            GROUP BY c.id, c.centroid
        )
        SELECT n.id, n.year, n.first, best.person_id, best.name, best.sim, best.second
        FROM nameless n
        CROSS JOIN LATERAL (
            SELECT k.person_id, pp.name, pp.born_on,
                   (1 - (k.centroid <=> n.centroid)) AS sim,
                   -- the next person along, so "clearly nearer" can be asked
                   (SELECT max(1 - (o.centroid <=> n.centroid))
                    FROM photo_face_clusters o
                    JOIN photo_people opp ON opp.id = o.person_id
                    JOIN photo_faces of2 ON of2.cluster_id = o.id
                    JOIN photos op ON op.id = of2.photo_id
                    WHERE o.person_id IS NOT NULL AND o.person_id <> k.person_id
                      AND o.centroid IS NOT NULL
                      AND abs(EXTRACT(YEAR FROM op.taken_at) - n.year) <= :within
                      AND (opp.born_on IS NULL OR n.first >= opp.born_on)
                   ) AS second
            FROM photo_face_clusters k
            JOIN photo_people pp ON pp.id = k.person_id
            JOIN photo_faces kf ON kf.cluster_id = k.id
            JOIN photos kp ON kp.id = kf.photo_id
            WHERE k.person_id IS NOT NULL AND k.centroid IS NOT NULL
              AND abs(EXTRACT(YEAR FROM kp.taken_at) - n.year) <= :within
              AND (pp.born_on IS NULL OR n.first >= pp.born_on)
            ORDER BY k.centroid <=> n.centroid
            LIMIT 1
        ) best
        WHERE best.sim >= :known
    """), {"compared": COMPARED, "within": WITHIN_YEARS, "known": KNOWN})).all()

    for row in rows:
        if row.second is not None and float(row.second) > float(row.sim) - CLEARLY:
            unsure += 1
            continue
        await session.execute(text("""
            UPDATE photo_face_clusters SET person_id = :pid WHERE id = :cid
        """), {"pid": row.person_id, "cid": row.id})
        await session.execute(text("""
            UPDATE photo_faces SET person_id = :pid WHERE cluster_id = :cid
        """), {"pid": row.person_id, "cid": row.id})
        named += 1
    return {"considered": len(rows), "named": named, "unsure": unsure}
