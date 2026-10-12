from opus.settings_store import RuntimeConfig
from opus.video.channels.base import Candidate
from opus.video.pipeline import score

USENET_FIRST = RuntimeConfig({"protocol_preference": "usenet_first"})


def offer(title: str, protocol: str, seeders: int | None = None,
          config: RuntimeConfig = USENET_FIRST) -> Candidate:
    c = Candidate(channel="sabnzbd" if protocol == "usenet" else "qbittorrent", title=title,
                  size=0, protocol=protocol, seeders=seeders)
    score.score_candidate(config, c, profile="1080p")
    return c


def test_the_same_post_comes_from_usenet_however_well_the_torrent_is_seeded():
    torrent = offer("The Graham Norton Show S34E01 1080p WEB h264-CBFM", "torrent", seeders=50)
    usenet = offer("The.Graham.Norton.Show.S34E01.1080p.WEB.h264-CBFM", "usenet")
    assert torrent.score > usenet.score
    assert score.ranked(USENET_FIRST, [torrent, usenet]) == [usenet, torrent]


def test_usenet_holding_only_a_lower_resolution_waits_behind_the_torrent():
    torrent = offer("Film 2020 1080p WEB-DL", "torrent", seeders=10)
    usenet = offer("Film.2020.720p.WEB-DL", "usenet")
    assert score.ranked(USENET_FIRST, [usenet, torrent]) == [torrent, usenet]


def test_a_flawed_copy_on_usenet_is_not_put_first():
    torrent = offer("Film 2020 1080p WEB-DL", "torrent", seeders=10)
    camera = offer("Film.2020.1080p.CAM", "usenet")
    assert camera.flawed
    assert score.ranked(USENET_FIRST, [camera, torrent]) == [torrent, camera]


def test_best_score_leaves_the_order_to_the_score():
    config = RuntimeConfig({"protocol_preference": "best_score"})
    torrent = offer("The Graham Norton Show S34E01 1080p WEB h264-CBFM", "torrent", seeders=50,
                    config=config)
    usenet = offer("The.Graham.Norton.Show.S34E01.1080p.WEB.h264-CBFM", "usenet", config=config)
    assert score.ranked(config, [usenet, torrent]) == [torrent, usenet]
