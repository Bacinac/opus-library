"""Groups that look like the same person across the years, shown as a life.

The clustering will not join two groups from different years, on purpose: two
sisters at the same age defeat any similarity. That refusal is right for a
machine and useless to a person, who wants to see the child at four beside the
same child at fourteen and say yes or no.

So this crosses what the clustering would not, and proposes rather than decides.
What holds it together is likeness that both sides agree on, and what keeps it
apart is the birth year each run works out for itself.
"""

from sqlalchemy import text

from opus.models import COMPARED, FACE_GENERATION
from opus.photos.people.ages import in_years

# How alike two groups from DIFFERENT years must be before they are proposed as
# the same person. Deliberately stricter than the cut that builds a group: this
# crosses the time windows that exist to keep sisters apart, so it only ever
# proposes, never decides.
#
# Above it, likeness is its own evidence and nothing else is asked. Requiring a
# link to be among each group's three closest as well punishes a complete life:
# a group's closest neighbours across time are its OWN other years, so the
# better assembled a person is, the more of their own segments crowd the next
# one out.
CHAIN = 0.62

# The cut for joining two runs that do not overlap in time at all.
#
# A person changes slowly and without stopping, so consecutive periods resemble
# each other closely — until one link falls under the cut and the rest of the
# life breaks off as a separate run. That is why one man came out as four.
#
# A gap is a different question from a rivalry: a face that changed enough to
# break its own run is still the same face, and the birth year rules out the
# sister case that the strict cut was defending against.
BRIDGE = 0.45

# How near two groups must be to be joined at the WEAK cut on likeness alone.
# Below the confident cut a resemblance needs a second reason; being each other's
# single closest is one, and agreeing on when the person was born is the other.
#
# Either, not both, because they fail in different places. A baby's first year
# looks little like her third — Jana's 2008 sits at 0.58 from a run that starts
# in 2010 — and it is never anyone's closest match, because the run's own
# adjacent years are closer to each other than any of them is to the baby. What
# it does have is a birth year that agrees to within two years. Her sister's 2008
# is closer still at 0.67, and is refused, because her birth year is seven years
# out. The rivalry cannot tell those two apart; the calendar can.
NEAREST = 1

# How many neighbours each group is asked for. Past a handful the extra ones are
# reached through the others anyway, because a run is built by chaining; this is
# the depth of the index lookup, not the reach of a life.
NEIGHBOURS = 12

# How many windows two runs may share and still be joined.
#
# One person holds about one group per year, so a weak candidate sitting in years
# a run does not cover is far more likely to be that person's missing years than
# an intruder — and one sitting squarely on years the run already holds is more
# likely to be somebody who was standing beside them. A single year in common is
# the grouping breaking inside a year, which is exactly what a bridge is for;
# more than one is two people, because two people are photographed together for
# years rather than once.
#
# Only weighed below the confident cut. Above it the likeness has already
# answered, and a person whose two halves overlap in time is a person whose
# grouping broke, not two people.
SHARED_WINDOWS = 1

# How far two groups' implied birth years may differ and still be one person.
#
# This is the rule the calendar could not give. Two sisters photographed at the
# same age are identical to a recogniser and their years do not overlap, so
# nothing else refuses to join them — a baby from 2005 and a baby from 2008 were
# chained as one life. But the date minus how old the face looks is a birth year,
# and it should be the same number from every photograph of somebody ever taken:
# 2004 for one of them and 2007 for the other.
#
# Two and a half years, because the estimate is not exact. Measured against known
# birth dates it lands within one to three years, and it is read from up to
# twenty-five faces a group so the random part is well under a year.
BIRTH_YEARS_APART = 2.5


# The runs, remembered.
#
# Assembling them is three seconds of index lookups and union-find over every
# group in the library, and it is the first thing the page asks for. It changes
# only when a group or a person does — nothing else can move it — so the answer
# is kept and the question that decides whether it is still good is a handful of
# counts and sums, which Postgres answers in milliseconds.
_runs: dict = {"key": None, "answer": None}


async def _shape_of_things(session) -> tuple:
    return tuple((await session.execute(text("""
        SELECT (SELECT count(*) FROM photo_face_clusters),
               (SELECT coalesce(max(id), 0) FROM photo_face_clusters),
               (SELECT count(*) FROM photo_face_clusters WHERE person_id IS NOT NULL),
               (SELECT coalesce(sum(person_id), 0) FROM photo_face_clusters),
               -- faces leaving a group move its centroid without moving a count
               (SELECT coalesce(sum(faces_at), 0) FROM photo_face_clusters),
               (SELECT count(*) FROM photo_people),
               (SELECT coalesce(sum(extract(epoch from born_on)::bigint), 0)
                  FROM photo_people),
               -- the names too. A rename moves no count and no sum, and the page
               -- would have gone on showing the old one until something else
               -- happened to change
               (SELECT coalesce(sum(hashtext(name)::bigint), 0) FROM photo_people)
    """))).one())


async def runs(session) -> list[dict]:
    shape = await _shape_of_things(session)
    if _runs["key"] == shape and _runs["answer"] is not None:
        return _runs["answer"]

    detail = await _groups(session)
    linked = _Linked(detail)
    linked.name(detail)
    linked.link(await _likenesses(session))

    out = [life for group in _chains(detail, linked).values()
           if (life := _life(group)) is not None]
    # Named runs first, and only then the biggest. Sorted by size alone, a
    # person cleaned down to her three real groups falls below whatever the page
    # asks for and vanishes — which is exactly backwards, because a run somebody
    # has already named is the one being worked on.
    out.sort(key=lambda c: (c["person"] is None, -c["faces"]))
    _runs["key"], _runs["answer"] = shape, out
    return out


async def _groups(session) -> list:
    return (await session.execute(text("""
        SELECT f.cluster_id, count(*),
               min(EXTRACT(YEAR FROM p.taken_at))::int,
               max(EXTRACT(YEAR FROM p.taken_at))::int,
               min(p.taken_at)::date, max(p.taken_at)::date,
               (SELECT f2.id FROM photo_faces f2
                 WHERE f2.cluster_id = f.cluster_id ORDER BY f2.score DESC LIMIT 1),
               c.person_id, pp.name, pp.born_on,
               -- the date on the photograph minus how old the face is, taken as
               -- a median so one bad reading cannot move it. Empty for a group
               -- of grown faces, where the model measures nothing — and a group
               -- is grown unless most of it reads as a child, because a handful
               -- of stray low readings in a group of adults would otherwise date
               -- the adults, and date them by the year of the photograph
               CASE WHEN count(*) FILTER (
                        WHERE (""" + in_years("f.apparent_age") + """) IS NOT NULL)
                      >= 0.5 * count(*) FILTER (WHERE f.apparent_age IS NOT NULL)
                    THEN percentile_cont(0.5) WITHIN GROUP (
                        ORDER BY EXTRACT(YEAR FROM p.taken_at)
                                 - (""" + in_years("f.apparent_age") + """)
                    ) FILTER (WHERE (""" + in_years("f.apparent_age") + """) IS NOT NULL)
               END AS born
        FROM photo_faces f
        JOIN photos p ON p.id = f.photo_id
        JOIN photo_face_clusters c ON c.id = f.cluster_id
        LEFT JOIN photo_people pp ON pp.id = c.person_id
        WHERE f.cluster_id IS NOT NULL AND f.generation = :gen
        GROUP BY f.cluster_id, c.person_id, pp.name, pp.born_on
        -- a group somebody named is in, whatever its size. The threshold exists
        -- to keep the page from being a wall of dust, and a group with a name on
        -- it is not dust: it is where a person said somebody is
        HAVING count(*) >= :compared OR c.person_id IS NOT NULL
    """), {"gen": FACE_GENERATION, "compared": COMPARED})).all()


async def _likenesses(session) -> list:
    """Every pair that could be the same person, in either direction of time.

    The rule used to be "who comes next", which needs each group to occupy one
    window. They do not: a face that hardly changes for twenty years lands in a
    dozen groups that all span those same twenty years, no two of them are ever
    disjoint, and so nothing chained at all — one person stayed twenty runs.

    Mutual candidacy takes the calendar's place as the guard against a blob.

    Asked of the index, group by group, rather than as a cross join. A thousand
    groups against each other is a million distances and took a second and a
    tenth of every refresh; the centroids are kept on the groups themselves,
    written by whatever changes a group's faces, and this is a thousand index
    lookups."""
    return (await session.execute(text("""
        WITH near AS (
            -- against the table itself, not a copy of it in a CTE: the index is
            -- on the table and a copy cannot be searched with it
            SELECT a.id AS a, n.id AS b, (1 - n.d) AS sim,
                   row_number() OVER (PARTITION BY a.id ORDER BY n.d) AS rn
            FROM photo_face_clusters a
            CROSS JOIN LATERAL (
                SELECT b.id, b.centroid <=> a.centroid AS d
                FROM photo_face_clusters b
                WHERE b.id <> a.id AND b.centroid IS NOT NULL
                  AND (b.faces_at >= :compared OR b.person_id IS NOT NULL)
                ORDER BY b.centroid <=> a.centroid LIMIT :k
            ) n
            WHERE a.centroid IS NOT NULL
              AND (a.faces_at >= :compared OR a.person_id IS NOT NULL)
              AND (1 - n.d) >= :bridge
        )
        SELECT n.a, n.b, n.sim, greatest(n.rn, m.rn) AS rank
        FROM near n JOIN near m ON m.a = n.b AND m.b = n.a
        WHERE n.a < n.b
        ORDER BY n.sim DESC
    """), {"compared": COMPARED, "bridge": BRIDGE, "k": NEIGHBOURS})).all()


class _Linked:
    """The groups joined into runs, one person to a run."""

    def __init__(self, detail: list):
        self.spans = {cid: (y1, y2) for cid, _, y1, y2, _, _, _, _, _, _, _ in detail}
        # a date somebody typed replaces the estimate rather than joining it: it
        # is not a better guess, it is the answer
        self.borns = {cid: (float(known.year) if known is not None else float(b))
                      for cid, _, _, _, _, _, _, _, _, known, b in detail
                      if known is not None or b is not None}
        self.certain = {cid for cid, _, _, _, _, _, _, _, _, known, _ in detail
                        if known is not None}
        self.parent: dict[int, int] = {}
        self.members: dict[int, list[int]] = {c: [c] for c in self.spans}
        # whose run each root is, carried along as runs merge rather than searched
        self.owns: dict[int, int] = {}
        # remembered per run: it is asked twice for every one of six thousand
        # links and each answer walks the whole run
        self.settled: dict[int, float | None] = {}
        self.covered: dict[int, set[int]] = {}

    def find(self, x: int) -> int:
        parent = self.parent
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def birth(self, root: int) -> float | None:
        """What a run says its person's birth year is.

        A typed date wins outright wherever there is one — the estimate exists
        because nobody had typed anything. Otherwise every group should agree and
        the median is what it settles on."""
        if root in self.settled:
            return self.settled[root]
        told = [self.borns[c] for c in self.members[root] if c in self.certain]
        if told:
            self.settled[root] = told[0]
        else:
            seen = sorted(self.borns[c] for c in self.members[root] if c in self.borns)
            self.settled[root] = seen[len(seen) // 2] if seen else None
        return self.settled[root]

    def windows(self, root: int) -> set[int]:
        """Which years a run already covers. Years, not pairs of them: a group is
        made inside one year now, so a run's coverage is exact and a candidate
        either fills a gap in it or does not.

        Remembered like the birth year, and for the same reason: it is asked of
        every weak link and each answer walks the whole run."""
        if root in self.covered:
            return self.covered[root]
        out: set[int] = set()
        for c in self.members[root]:
            y1, y2 = self.spans.get(c, (None, None))
            if y1 is None:
                continue
            out.update(range(y1, (y2 if y2 is not None else y1) + 1))
        self.covered[root] = out
        return out

    def dated(self, ra: int, rb: int) -> bool:
        """Whether both runs know when their person was born. Only then can the
        birth year stand as a reason to join, rather than merely as a veto."""
        return self.birth(ra) is not None and self.birth(rb) is not None

    def join(self, a: int, b: int, fills_a_gap: bool = False) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        oa, ob = self.owns.get(ra), self.owns.get(rb)
        if oa is not None and ob is not None and oa != ob:
            return                # two people have been named; that is the answer
        ba, bb = self.birth(ra), self.birth(rb)
        if ba is not None and bb is not None and abs(ba - bb) > BIRTH_YEARS_APART:
            return                # born years apart: two people, however alike
        if fills_a_gap and len(self.windows(ra) & self.windows(rb)) > SHARED_WINDOWS:
            return                # standing beside them, not missing from them
        self.parent[rb] = ra
        self.members[ra].extend(self.members[rb])
        del self.members[rb]
        self.settled.pop(ra, None)
        self.settled.pop(rb, None)
        self.covered.pop(ra, None)
        self.covered.pop(rb, None)
        if oa is None and ob is not None:
            self.owns[ra] = ob
        self.owns.pop(rb, None)

    def name(self, detail: list) -> None:
        """A name comes first and answers to nothing.

        Groups given to the same person are one life before any likeness is
        consulted, and no rule here may refuse them: a resemblance is a guess and
        a name is a decision. Without this the view showed the same person on
        several rows — thirty-three groups all called Eva, scattered across the
        page by the very measure her name was meant to overrule."""
        named: dict[int, list[int]] = {}
        for cid, _n, _y1, _y2, _d1, _d2, _cover, pid, _pname, _known, _born in detail:
            if pid is not None:
                named.setdefault(pid, []).append(cid)
        for pid, group in named.items():
            for other in group[1:]:
                ra, rb = self.find(group[0]), self.find(other)
                if ra == rb:
                    continue
                self.parent[rb] = ra
                self.members[ra].extend(self.members[rb])
                del self.members[rb]
            self.owns[self.find(group[0])] = pid

    def link(self, edges: list) -> None:
        """Strongest first, and the confident cut before the bridging one: a run
        that has already settled who it is refuses a weaker claim on it, rather
        than the order of arrival deciding."""
        for a, b, sim, _rank in edges:
            if sim >= CHAIN:
                self.join(a, b)
        for a, b, sim, rank in edges:
            if sim >= CHAIN:
                continue
            if rank <= NEAREST or self.dated(self.find(a), self.find(b)):
                self.join(a, b, fills_a_gap=True)


def _chains(detail: list, linked: _Linked) -> dict[int, list[dict]]:
    chains: dict[int, list] = {}
    for cid, n, y1, y2, d1, d2, cover, pid, pname, known, born in detail:
        chains.setdefault(linked.find(cid), []).append(
            {"id": cid, "faces": n, "years": [y1, y2], "cover": cover,
             "first": d1.isoformat() if d1 else None,
             "last": d2.isoformat() if d2 else None,
             "person": {"id": pid, "name": pname} if pid else None,
             "born": known.year if known is not None
                     else (round(float(born)) if born is not None else None),
             "certain": known is not None})
    return chains


def _life(group: list[dict]) -> dict | None:
    # one group is not a life — unless a person put their name on it, and
    # then it is the only place they appear
    if len(group) < 2 and not any(m["person"] for m in group):
        return None
    group.sort(key=lambda m: (m["years"][0] or 0, m["id"]))
    whose = next((m["person"] for m in group if m["person"]), None)
    told = [m["born"] for m in group if m.get("certain")]
    years = sorted(m["born"] for m in group if m["born"] is not None)
    eras = _eras(group)
    seen = sorted(m["first"] for m in group if m["first"])
    gone = sorted(m["last"] for m in group if m["last"])
    return {
        # the years, in order, which is the person changing; and separately
        # every group as it is stored, because the two are different things
        # to look at and one is not a summary of the other
        "groups": eras,
        "clusters": [{k: m[k] for k in ("id", "faces", "cover", "person")}
                     | {"year": m["years"][0]} for m in group],
        "faces": sum(m["faces"] for m in group),
        "years": [eras[0]["year"], eras[-1]["year"]],
        # the actual days, because a run of years with a dash through it
        # reads as a lifespan and these are facts about photographs
        "first": seen[0] if seen else None,
        "last": gone[-1] if gone else None,
        "person": whose,
        # worked out, not typed in: the same number from every photograph of
        # them, which is what makes it worth showing before anyone is named
        "born": told[0] if told else (years[len(years) // 2] if years else None),
        "certain": bool(told),
    }


def _eras(group: list[dict]) -> list[dict]:
    """A period is a year, not a group. Groups are made inside a year and one
    person still ends up with several of them there — the small ones especially,
    and a named person keeps every one however small. Shown as they are stored, a
    well assembled life reads as fifty-five periods across twenty-five years,
    which is not what a period means. The groups stay separate underneath; a tile
    carries every group it stands for, so choosing one still chooses all of
    them."""
    by_year: dict[int, dict] = {}
    for member in group:
        year = member["years"][0]
        era = by_year.setdefault(year, {
            "year": year, "id": member["id"], "faces": 0, "ids": [],
            "cover": member["cover"], "largest": -1,
            "person": member["person"],
        })
        era["faces"] += member["faces"]
        era["ids"].append(member["id"])
        if member["faces"] > era["largest"]:
            era["largest"] = member["faces"]
            era["cover"] = member["cover"]
            era["id"] = member["id"]
        if member["person"] and not era["person"]:
            era["person"] = member["person"]
    return [{k: v for k, v in era.items() if k != "largest"}
            for _, era in sorted(by_year.items(),
                                 key=lambda kv: (kv[0] is None, kv[0]))]
