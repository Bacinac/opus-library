import random
from array import array
from types import SimpleNamespace

import pytest

from opus.api.routers.video import shared
from opus.video import credits, library_scan
from opus.video.metadata import awards


def chapter(title, start):
    return SimpleNamespace(title=title, start_s=start)


@pytest.mark.parametrize(("chapters", "found"), [
    ([chapter("Intro", 0), chapter("End Credits", 2500)], 2500),
    ([chapter("Credits", 2900), chapter("closing credits", 2600)], 2600),
    # another cut's table: its credits begin where this file has already ended
    ([chapter("Intro", 0), chapter("Credits", 2700), chapter("Scene 9", 3100)], None),
    ([chapter("Opening Credits", 60)], None),
    ([chapter("Credits", 1000)], None),
    ([chapter("Chapter 12", 2700)], None),
])
def test_a_chapter_counts_only_when_it_names_the_credits_near_the_end(chapters, found):
    assert credits.from_chapters(chapters, 3000.0) == found


def season_of(offsets, theme_length, seed=7):
    """Episodes whose tails are noise apart from one theme they all share, each
    starting it at its own place."""
    rnd = random.Random(seed)
    theme = [rnd.getrandbits(32) for _ in range(theme_length)]
    prints = {}
    for key, (start, at) in offsets.items():
        values = [rnd.getrandbits(32) for _ in range(600)]
        values[at:at + theme_length] = theme
        prints[key] = (start, array("I", values))
    return prints


def test_the_shared_theme_says_where_each_episode_closes():
    found = credits.from_sound(season_of({1: (1000.0, 300), 2: (1200.0, 100), 3: (900.0, 420)}, 150))
    assert found == pytest.approx({1: 1000.0 + 300 * credits.FP_ITEM_S,
                                   2: 1200.0 + 100 * credits.FP_ITEM_S,
                                   3: 900.0 + 420 * credits.FP_ITEM_S}, abs=credits.FP_ITEM_S)


def test_a_cue_shorter_than_a_theme_decides_nothing():
    assert credits.from_sound(season_of({1: (0.0, 50), 2: (0.0, 200)}, credits.FP_RUN // 2)) == {}


@pytest.mark.parametrize(("chapters", "found"), [
    ([chapter("Previously On", 0), chapter("Opening Credits", 300), chapter("Scene 2", 361)], [(300, 361)]),
    ([chapter("4. Title Sequence", 90), chapter("5. Scene", 150)], [(90, 150)]),
    ([chapter("Intro", 7), chapter("Chapter 1", 63), chapter("Intro", 441), chapter("Chapter 2", 485)],
     [(7, 63), (441, 485)]),
    # a chapter that begins the file and runs six minutes is the cold open as well
    ([chapter("Intro", 0), chapter("Scene 1", 347)], []),
    ([chapter("Intro", 500), chapter("Scene 1", 509)], []),
    ([chapter("Intro", 3000), chapter("Scene 9", 3060)], []),
    ([chapter("Opening Credits", 300)], []),
    ([chapter("Intro", 12), chapter("Chapter 2", 64), chapter("Credits", 7115)], []),
])
def test_a_chapter_names_the_titles_only_near_the_start_and_as_long_as_titles(chapters, found):
    assert credits.intro_chapters(chapters, 3600.0) == found


@pytest.mark.parametrize(("chapters", "sound", "found"), [
    # Silo: the marker stops at the sequence, the music runs on through the cards
    ([(220.0, 297.0)], (218.0, 314.0), (220.0, 314.0)),
    # Lioness: the first "Intro" is the story before the titles
    ([(6.6, 63.3), (441.2, 485.4)], (439.4, 499.3), (441.2, 499.3)),
    # Mrs. America: one table for every episode, and this episode's titles elsewhere
    ([(162.2, 200.0)], (244.5, 304.3), (244.5, 304.3)),
    # The White Lotus: the table half a minute off, and the titles right after the logo
    ([(35.9, 138.1)], (0.4, 109.4), (0.4, 109.4)),
    ([], (0.1, 106.3), (0.1, 106.3)),
    # titles no other episode of the season shares are not titles to skip
    ([(300.0, 361.0)], None, None),
])
def test_the_titles_are_where_the_season_hears_them_and_a_chapter_only_refines_them(chapters, sound, found):
    assert credits.intro_between(chapters, sound) == found


@pytest.mark.parametrize(("chapters", "sound", "foreign"), [
    ([chapter("Intro", 162.2), chapter("Main", 200.0), chapter("Credits", 2678.6)], (244.5, 304.3), True),
    ([chapter("Intro", 3612.5), chapter("Chapter 2", 3664.4), chapter("Credits", 7115.0)], None, True),
    ([chapter("Intro", 220.0), chapter("Scene 1", 297.0), chapter("Credits", 2900.0)], (218.0, 314.0), False),
    ([chapter("Intro", 162.2), chapter("Main", 200.0), chapter("Credits", 2678.6)], None, False),
    ([], (0.1, 106.3), False),
])
def test_a_table_that_misplaces_the_titles_or_outruns_the_file_is_another_cut(chapters, sound, foreign):
    assert credits.borrowed(chapters, 3600.0, sound) is foreign


def opening_of(offsets, theme_length, stray=(), length=4000, seed=11):
    """Episodes of noise sharing one title sequence, each at its own offset, and
    a few single values agreeing just outside it — the way two episodes' sound
    agrees by chance for an instant at either edge of a short title card."""
    rnd = random.Random(seed)
    theme = [rnd.getrandbits(32) for _ in range(theme_length)]
    edge = [rnd.getrandbits(32) for _ in range(40)]
    prints = {}
    for key, at in offsets.items():
        values = [rnd.getrandbits(32) for _ in range(length)]
        values[at:at + theme_length] = theme
        for k in stray:
            values[at + k] = edge[k % len(edge)]
        prints[key] = array("I", values)
    return prints


def test_the_shared_titles_are_found_wherever_the_story_before_them_ends():
    length = int(60 / credits.FP_ITEM_S)
    offsets = {1: 300, 2: 2500, 3: 900}
    found = credits.intro_from_sound(opening_of(offsets, length))
    assert found.keys() == offsets.keys()
    for key, at in offsets.items():
        assert found[key] == pytest.approx(
            (at * credits.FP_ITEM_S, (at + length) * credits.FP_ITEM_S), abs=2 * credits.FP_ITEM_S)


def test_a_stray_agreement_beside_a_title_card_does_not_stretch_it_into_one():
    length = int(10 / credits.FP_ITEM_S)
    gap = int(2.5 / credits.FP_ITEM_S)
    stray = (-2 * gap, -gap, length + gap, length + 2 * gap)
    assert credits.intro_from_sound(opening_of({1: 300, 2: 800}, length, stray)) == {}


def test_two_copies_of_one_episode_share_everything_and_so_no_titles():
    prints = opening_of({1: 0, 2: 0}, 3000)
    assert credits.intro_from_sound(prints) == {}


NAVBOX = """{{Navbox
| list1 =
* ''[[Parasite (2019 film)|Parasite]]'' (2019)
* ''[[Titane (film)|Titane]]'' {{small|[[74th Cannes Film Festival|74th]]}} (2021)
* ''[[The Umbrellas of Cherbourg]]'' and ''[[Film B]]'' (1963–1964)
<!-- * ''[[Hidden]]'' (2000) -->
* ''[[No Year Given]]''
* [[Not a winner]] (2001)
}}"""


def test_a_navbox_gives_each_winner_with_its_year():
    assert awards.entries(NAVBOX) == [
        (2019, "Parasite (2019 film)"),
        (2021, "Titane (film)"),
        (1964, "The Umbrellas of Cherbourg"),
        (1964, "Film B"),
    ]


def test_a_title_split_or_glued_by_the_release_is_tried_every_way():
    assert library_scan._title_variants({"title": "Anatomie d'une chute AKA Anatomy of a Fall"}) == [
        "Anatomie d'une chute AKA Anatomy of a Fall", "Anatomie d'une chute", "Anatomy of a Fall"]
    assert library_scan._title_variants(
        {"title": "The Lord of the Rings", "alternative_title": "The Rings of Power"}) == [
        "The Lord of the Rings The Rings of Power", "The Lord of the Rings", "The Rings of Power"]


def test_a_match_needs_the_title_not_a_near_miss():
    bronk = {"title": "Bronk", "original_title": "Bronk", "year": 1975}
    bron = {"title": "Bron", "original_title": "Bron/Broen", "year": 2011}
    assert library_scan._match([("Bron", [bronk])], None) is None
    assert library_scan._match([("Bron", [bronk, bron])], 2011) is bron
    lioness = {"title": "Lioness", "original_title": "Lioness", "year": 2023}
    assert library_scan._match([("Special Ops Lioness", [lioness])], None) is lioness
    far_down = [{"title": f"Other {n}", "original_title": "", "year": None} for n in range(6)]
    assert library_scan._match([("Lioness", far_down + [lioness])], None) is None


@pytest.mark.parametrize(("width", "height", "tier"), [
    (1920, 960, "1080p"), (3840, 1632, "2160p"), (1280, 534, "720p"), (720, 576, "480p"),
    (1440, 1080, "1080p"), (640, 360, "640×360"), (None, None, ""),
])
def test_a_scope_frame_is_named_by_whichever_way_it_reaches(width, height, tier):
    assert shared._resolution(SimpleNamespace(width=width, height=height)) == tier


def test_subtitles_list_the_house_languages_first():
    subs = [{"lang": lang} for lang in ("sv", "en", "de", "hr", "ar")]
    assert [s["lang"] for s in sorted(subs, key=shared._sub_sort_key)] == ["hr", "en", "ar", "de", "sv"]


def test_an_overview_falls_back_to_english_rather_than_nothing():
    film = SimpleNamespace(overview="English.", overview_hr="Hrvatski.")
    untranslated = SimpleNamespace(overview="English.", overview_hr="")
    assert shared.overview_in(film, "hr") == "Hrvatski."
    assert shared.overview_in(untranslated, "hr") == "English."
    assert shared.overview_in(film, "en") == "English."
