from pathlib import Path

from opus.video.library_scan import _best, _match, _norm, movie_guess

ROOT = Path("/movies")


def test_a_disc_stream_is_named_by_its_folder():
    guess = movie_guess(ROOT / "Eternal Sunshine of the Spotless Mind (2004)" / "00000.m2ts", ROOT)
    assert (guess["title"], guess["year"]) == ("Eternal Sunshine of the Spotless Mind", 2004)


def test_a_release_name_in_its_folder_is_still_the_film():
    guess = movie_guess(ROOT / "Heat (1995)" / "Heat.1995.2160p.UHD.BluRay.REMUX.HDR.HEVC-FGT.mkv", ROOT)
    assert (guess["title"], guess["year"]) == ("Heat", 1995)


def test_a_file_outside_the_library_is_read_by_its_name():
    guess = movie_guess(Path("/elsewhere/Heat.1995.1080p.mkv"), ROOT)
    assert (guess["title"], guess["year"]) == ("Heat", 1995)


def test_an_accent_is_folded_not_dropped():
    assert _norm("TÁR") == "tar"
    assert _norm("Čovjek koji je volio brodove") == "covjek koji je volio brodove"
    assert _norm("Брат") == "брат"


def test_the_year_decides_between_two_films_of_one_name():
    results = [{"tmdb_id": 515466, "title": "Tar", "original_title": "Tar", "year": 2020},
               {"tmdb_id": 817758, "title": "TÁR", "original_title": "TÁR", "year": 2022}]
    assert _match([("TÁR", results)], 2022)["tmdb_id"] == 817758


def test_the_exact_name_in_its_year_counts_however_far_down_it_is():
    results = [{"tmdb_id": i, "title": f"Help {i}", "original_title": "", "year": 2000}
               for i in range(6)]
    results.insert(2, {"tmdb_id": 14831, "title": "Help!", "original_title": "Help!", "year": 1965})
    results.append({"tmdb_id": 826796, "title": "Help", "original_title": "Help", "year": 2021})
    assert _match([("Help", results)], 2021)["tmdb_id"] == 826796


def test_two_films_of_one_name_and_year_are_a_tie():
    results = [{"tmdb_id": 1591303, "title": "Trust", "original_title": "Trust", "year": 2025},
               {"tmdb_id": 1244953, "title": "Trust", "original_title": "Trust", "year": 2025}]
    assert [r["tmdb_id"] for r in _best([("Trust", results)], 2025)] == [1591303, 1244953]
