from types import SimpleNamespace

from conftest import library, run, signed_in
from opus import db
from opus.models import Movie, Series
from opus.settings_store import RuntimeConfig
from opus.video.subtitles.policy import (PolicyResult, SubtitlePolicy, effective_policy, evaluate,
                                         parse_override, policy_met)


def config(mode="any", langs="en,hr"):
    return RuntimeConfig({"subtitle_mode": mode, "subtitle_langs": langs})


def test_any_is_met_by_one_wanted_language():
    result = evaluate(SubtitlePolicy("any", ("en", "hr")), [{"lang": "hr"}, {"lang": "de"}])
    assert result == PolicyResult(True, (), ("hr",))


def test_any_with_none_of_the_wanted_languages_misses_all_of_them():
    result = evaluate(SubtitlePolicy("any", ("hr", "en")), [{"lang": "de"}])
    assert result == PolicyResult(False, ("en", "hr"), ())


def test_all_names_exactly_the_languages_still_missing():
    result = evaluate(SubtitlePolicy("all", ("en", "hr", "sl")), [{"lang": "en"}, {"lang": "fr"}])
    assert result == PolicyResult(False, ("hr", "sl"), ("en",))


def test_all_is_met_only_when_every_language_is_there():
    result = evaluate(SubtitlePolicy("all", ("en", "hr")), [{"lang": "hr"}, {"lang": "en"}])
    assert result == PolicyResult(True, (), ("en", "hr"))


def test_none_waives_the_requirement_and_reports_what_is_there():
    result = evaluate(SubtitlePolicy("none", ("en",)), [{"lang": "de"}])
    assert result == PolicyResult(True, (), ("de",))


def test_a_policy_with_no_languages_asks_for_nothing():
    assert evaluate(SubtitlePolicy("all", ()), []).satisfied


def test_an_automatic_track_does_not_count_unless_accepted():
    subs = [{"lang": "en", "auto_generated": True}]
    policy = SubtitlePolicy("any", ("en",))
    assert evaluate(policy, subs) == PolicyResult(False, ("en",), ())
    assert evaluate(policy, subs, accept_auto=True) == PolicyResult(True, (), ("en",))


def test_rows_are_read_like_dicts():
    rows = [SimpleNamespace(lang="hr", auto_generated=False),
            SimpleNamespace(lang="en", auto_generated=True)]
    assert evaluate(SubtitlePolicy("all", ("en", "hr")), rows).missing == ("en",)


def test_an_override_is_parsed_whatever_its_case_and_spacing():
    assert parse_override("  ALL:en,hr ") == SubtitlePolicy("all", ("en", "hr"))
    assert parse_override("none") == SubtitlePolicy("none", ())
    assert parse_override("any:eng") == SubtitlePolicy("any", ("eng",))


def test_a_malformed_override_is_no_override():
    for spec in ("", None, "some:en", "all:", "all:en,", "all:e", "all:english", "all en",
                 "all", "any", "none:en"):
        assert parse_override(spec) is None, spec


def test_the_item_override_wins_over_the_install():
    assert effective_policy(config("any", "en,hr"), "all:sl") == SubtitlePolicy("all", ("sl",))


def test_without_a_valid_override_the_install_decides():
    install = config("all", " en , hr ,")
    assert effective_policy(install) == SubtitlePolicy("all", ("en", "hr"))
    assert effective_policy(install, "bogus") == SubtitlePolicy("all", ("en", "hr"))


def test_an_item_is_complete_when_its_languages_meet_the_policy():
    assert policy_met(config("any"), "", ["hr"])
    assert not policy_met(config("all"), "", ["hr"])
    assert policy_met(config("all"), "none", [])
    assert not policy_met(config("none"), "any:en", None)


def test_an_override_the_policy_cannot_read_is_refused_at_the_door(quick):
    async def scenario():
        cookie = await signed_in("boss")
        async with db.SessionLocal() as session:
            movie = Movie(tmdb_id=1, title="Film", year=2020)
            series = Series(tmdb_id=2, title="Show", year=2020)
            session.add_all([movie, series])
            await session.commit()
            ids = {"movies": movie.id, "series": series.id}
        answers = {}
        async with library(cookie) as client:
            for kind, item in ids.items():
                for spec in ("bogus", "all", "any", "none:en", " ALL:en,HR ", "none", ""):
                    answer = await client.patch(f"/api/video/{kind}/{item}",
                                                json={"subtitle_override": spec})
                    async with db.SessionLocal() as session:
                        row = await session.get(Movie if kind == "movies" else Series, item)
                        answers[kind, spec] = (answer.status_code, row.subtitle_override)
        return answers

    answers = run(scenario())
    for kind in ("movies", "series"):
        assert [answers[kind, spec][0] for spec in ("bogus", "all", "any", "none:en")] == [422] * 4
        assert answers[kind, " ALL:en,HR "] == (200, "all:en,hr")
        assert answers[kind, "none"] == (200, "none")
        assert answers[kind, ""] == (200, "")
