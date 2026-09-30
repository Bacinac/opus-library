"""Runtime, user-editable configuration persisted in Postgres and edited via
the Settings page. Distinct from opus.config (infra bootstrap from env:
database URL, poll cadence). Labels live in the frontend i18n catalog; this
module only defines keys, groups and validation.

The groups are what the Settings page draws as cards, and they say where the
merge is real and where it is not: `acquire` and `access` belong to the whole
installation — one address to OPUS · Downloads instead of one per half, one
login where there was none — while everything else is prefixed by the half it
belongs to, because a subtitle policy has nothing to say about an album and a
lossless ceiling has nothing to say about a film."""

import re
from collections.abc import Callable
from pathlib import Path

from opus_core.settings import RuntimeConfig as _RuntimeConfig
from opus_core.settings import SettingSpec, SettingsValidationError, Store

from opus.db import SessionLocal
from opus.models import Setting
from opus.music.matching.quality import CEILINGS
from opus.plugins import CHANNELS, SETTINGS

MUSIC_NAMING_TOKENS = {"artist", "album", "year", "nn", "title"}
MOVIE_NAMING_TOKENS = {"title", "year"}
EPISODE_NAMING_TOKENS = {"series", "year", "ss", "ee", "title"}
# the music half's acquisition channels, a plugin's after the built-in ones
MUSIC_CHANNELS = ("slskd", "sabnzbd", *(name for name, _ in CHANNELS))
LANGS_RE = re.compile(r"^[a-z]{2,3}(,[a-z]{2,3})*$")
# "1080p:4,2160p:10" — a rate per resolution, in megabits per second
BITRATES_RE = re.compile(r"^\d{3,4}p:\d+(\.\d+)?(,\d{3,4}p:\d+(\.\d+)?)*$")


SETTINGS_SPEC: tuple[SettingSpec, ...] = (
    # Everything acquired comes through OPUS · Downloads: one address and one
    # token for the library, where the two apps each carried their own.
    SettingSpec("opus_url", "acquire", ""),
    SettingSpec("opus_token", "acquire", "", secret=True),
    # the one landing tree, under the two names its two mounts give it: OPUS
    # writes into /landing and the library reads the same host directory at
    # /downloads
    SettingSpec("opus_landing_root", "acquire", "/landing"),
    SettingSpec("opus_landing_dir", "acquire", "/downloads"),
    SettingSpec("cleanup_after_import", "acquire", "true", kind="bool"),
    # how long a folder nobody imported may sit in the landing zone. The
    # import-time cleanup only covers what SUCCEEDS; everything refused or
    # interrupted stayed forever, and forever reached 123 GB.
    SettingSpec("landing_keep_days", "acquire", "14", kind="number"),

    SettingSpec("music_dir", "music_library", "/music"),
    SettingSpec("music_naming", "music_library", "{artist}/{album} ({year})/{nn} - {title}"),
    SettingSpec(
        "release_filter", "music_library", "albums",
        kind="select", options=("albums", "albums_eps", "all"),
    ),
    SettingSpec(
        "music_quality_profile", "music_acquire", "prefer_lossless",
        kind="select", options=("prefer_lossless", "lossless_only", "any"),
    ),
    # no ceiling by default: this library already holds 1827 tracks at 24/192
    # and 84 at 24/176, so capping lower would refuse what it deliberately keeps
    SettingSpec("max_quality", "music_acquire", "any", kind="select", options=tuple(CEILINGS)),
    SettingSpec("min_candidate_score", "music_acquire", "40", kind="number"),
    SettingSpec("channel_order", "music_acquire", ",".join(MUSIC_CHANNELS), options=MUSIC_CHANNELS),
    SettingSpec("spotify_client_id", "music_metadata", ""),
    SettingSpec("spotify_client_secret", "music_metadata", "", secret=True),
    SettingSpec("discogs_token", "music_metadata", "", secret=True),

    # The photo half reads this tree and never writes to it. It is bound
    # read-only in the compose, which is what makes that a property of the
    # installation rather than a promise made by the code.
    SettingSpec("photos_dir", "photos_library", "/photos"),
    # A camera that records no offset leaves a wall clock. Reading it in the
    # zone the household lives in is right for almost everything and wrong for
    # the holidays; the photograph records that the offset was missing, so the
    # two can be told apart later.
    SettingSpec("photos_timezone", "photos_library", "Europe/Zagreb"),
    # A cache of a computation over files we already hold. It has a volume of
    # its own so the photo tree can stay mounted read-only, and it declares
    # itself a cache so no backup carries it.
    SettingSpec("photos_derivatives_dir", "photos_library", "/derivatives"),
    # How many photographs are encoded at once. Four on a four-core box: libvips
    # is told to use one thread each, so the parallelism is across pictures
    # rather than inside one, which is the right way round for a corpus.
    SettingSpec("photos_derive_workers", "photos_library", "4", kind="number"),
    # The private half: what each person's phone puts away, encrypted before it
    # is sent and unreadable here. Its own volume because it is neither the
    # shared tree (which is read-only and belongs to the household) nor a cache
    # (this is the only copy once a phone is lost), and because it is the one
    # directory in the install that a backup MUST carry.
    SettingSpec("photos_vault_dir", "photos_library", "/vault"),
    # The same tree as above, bound a second time and writable. Everything that
    # reads goes through the read-only name and physically cannot write; the two
    # things that do write — taking in an offered picture, and moving it to the
    # year it turned out to belong to — go through this one. The catalogue only
    # ever records the read-only name, so a path in the database is always the
    # path the pass will look for.
    SettingSpec("photos_write_dir", "photos_library", "/photos-write"),
    # Where the faces service answers. It is a separate container because it
    # needs Intel's runtime for the card, which our Debian does not carry — the
    # address is a setting rather than a constant for the same reason every
    # other engine's is: an install may put it somewhere else.
    SettingSpec("faces_url", "photos_library", "http://faces:8099"),

    # The four numbers that decide who ends up grouped with whom. They are here
    # rather than in the code because the right values are a property of one
    # household's photographs — how many children, how close in age, how many
    # years — and the only way to find them is to look at the result.
    #
    # How alike two faces must be before the machine will connect them. Measured
    # over 4,320 of this library's faces: the same person averages 0.479 and two
    # different people 0.013, but one pair of DIFFERENT people in ten thousand
    # reaches 0.516 — so this is not a number to take from the middle.
    SettingSpec("faces_threshold", "photos_people", "0.50", kind="number"),
    # How many close neighbours a face needs before it may join two groups
    # together. Raise it when strangers leak in; lower it when one person breaks
    # into too many groups. This is the rule that stops one ambiguous face
    # welding two people together for good.
    SettingSpec("faces_min_core", "photos_people", "3", kind="number"),
    # Faces are grouped inside a window of this many years and never across one.
    # The year of the photograph is the one exact thing in this whole problem,
    # and a window of one keeps it exact: a group that spans two years puts a
    # year of slop into the birth year worked out from it. Widen it only for an
    # archive so sparse that a single year holds too little to group.
    SettingSpec("faces_window_years", "photos_people", "1", kind="number"),
    # How many neighbours each face is asked for. Rarely worth changing: past
    # the size of a family gathering the extra ones are reached through the
    # others anyway.
    SettingSpec("faces_neighbours", "photos_people", "20", kind="number"),
    # How close a lone face must be to a group before it is taken into it.
    # Looser than the cut that builds a group, because the question is easier:
    # not "do these two belong together" but "does this one belong to that
    # established crowd". A lone face joining cannot chain two people — it
    # connects nothing to nothing — so this is a gain without the usual risk.
    SettingSpec("faces_adopt", "photos_people", "0.42", kind="number"),
    # How alike two groups from the SAME year must be to become one group. The
    # graph is built from each face's nearest few, so somebody photographed all
    # day lands in an island per occasion rather than a group per person; this
    # is the cut that puts those back together. Measured on pairs of groups from
    # one year whose people are known: the same person averages 0.627, two
    # different people 0.028.
    SettingSpec("faces_weld", "photos_people", "0.45", kind="number"),
    # How much of their photographs two groups may share and still be joined.
    # Two people photographed together for years share most of theirs; two
    # halves of one person share the odd stray carried in by a misplaced face.
    SettingSpec("faces_weld_shared", "photos_people", "0.05", kind="number"),
    # What share of the links between two groups must clear that cut before they
    # are joined. Joining on a single link is what welded a mother to her two
    # daughters: the links were 99 % right and 204 wrong ones out of 20,735 were
    # enough, because three of them join three people. Two in five is measured:
    # purity stays at 98.5 % and five groups per person per year become one and
    # a half. Raise it toward 1 for caution, lower it to consolidate harder.
    SettingSpec("faces_weld_agree", "photos_people", "0.40", kind="number"),
    # How much detail a face must carry to take part at all. Not a preference
    # about quality but about correctness: a face out of focus still produces a
    # vector, and that vector sits near every other out-of-focus face, so blur is
    # itself a resemblance and everyone ever mis-focused assembles into one
    # person made of smears. Measured at a fixed size, so a small distant face
    # fails the same test as a large blurred one.
    SettingSpec("faces_min_sharpness", "photos_people", "0", kind="number"),

    SettingSpec("movies_dir", "video_library", "/movies"),
    SettingSpec("tv_dir", "video_library", "/television"),
    SettingSpec("video_dir", "video_library", "/video"),
    SettingSpec("movie_naming", "video_library", "{title} ({year})/{title} ({year})"),
    SettingSpec("episode_naming", "video_library",
                "{series}/Season {ss}/{series} - S{ss}E{ee} - {title}"),
    SettingSpec(
        "protocol_preference", "video_acquire", "usenet_first",
        kind="select", options=("usenet_first", "torrent_first", "best_score"),
    ),
    # Two profiles, because a film and an episode are not the same purchase: a
    # film is watched once on the best picture the house can carry, a series is
    # forty hours of it and 4K buys forty times the disk for the same sofa.
    SettingSpec(
        "movie_quality_profile", "video_acquire", "2160p",
        kind="select", options=("2160p", "1080p", "720p", "any"),
    ),
    SettingSpec(
        "tv_quality_profile", "video_acquire", "1080p",
        kind="select", options=("2160p", "1080p", "720p", "any"),
    ),
    # Megabits per second, per resolution. Size on its own says nothing — eight
    # gigabytes is a remuxed episode and a bad film — but size over running time
    # is the same number for both, and it is knowable before anything is
    # fetched. Under the floor is an upscale or a mess; over the ceiling is disk
    # spent on what the television cannot show.
    SettingSpec("bitrate_floor", "video_acquire", "720p:1.5,1080p:4,2160p:10"),
    SettingSpec("bitrate_ceiling", "video_acquire", "720p:12,1080p:28,2160p:80"),
    SettingSpec("prefer_hdr", "video_acquire", "true", kind="bool"),
    SettingSpec("prefer_surround", "video_acquire", "true", kind="bool"),
    # the services the house pays for, as TMDB's provider ids
    SettingSpec("streaming_subscriptions", "video_acquire", "", kind="list"),
    SettingSpec("category_movies", "video_acquire", "movies"),
    SettingSpec("category_tv", "video_acquire", "tv"),
    SettingSpec("ytdlp_format", "video_acquire", "bestvideo*+bestaudio/best"),
    SettingSpec("sponsorblock", "video_acquire", "false", kind="bool"),
    SettingSpec("subtitle_langs", "video_subtitles", "en,hr"),
    SettingSpec(
        "subtitle_mode", "video_subtitles", "any",
        kind="select", options=("any", "all", "none"),
    ),
    SettingSpec("accept_auto_subs", "video_subtitles", "false", kind="bool"),
    SettingSpec("opensubtitles_api_key", "video_subtitles", "", secret=True),
    SettingSpec("opensubtitles_username", "video_subtitles", ""),
    SettingSpec("opensubtitles_password", "video_subtitles", "", secret=True),
    SettingSpec("tmdb_api_key", "video_metadata", "", secret=True),

    # Where the household keeps its address book. DIDA is the house's ONE reader
    # of it — one consent screen, one token to expire, one schedule — and this
    # asks. A birth date is needed here for a single thing: two children who look
    # alike at the same age are told apart by nothing else.
    SettingSpec("dida_url", "photos_contacts", ""),
    SettingSpec("dida_username", "photos_contacts", ""),
    SettingSpec("dida_password", "photos_contacts", "", secret=True),
    SettingSpec("dida_panel_key", "photos_contacts", "", secret=True),

    # the tokens the consuming modules carry instead of a session, one each. Who
    # may sign in is not a setting: the roster is its own table.
    SettingSpec("access_player_token", "access", "", secret=True),
    SettingSpec("access_downloads_token", "access", "", secret=True),
    SettingSpec("access_cameras_token", "access", "", secret=True),
) + SETTINGS
DIR_KEYS = ("music_dir", "movies_dir", "tv_dir", "video_dir", "photos_dir",
            "opus_landing_dir")
# template key -> (tokens it may use, tokens it must use)
_TEMPLATES = {
    "music_naming": (MUSIC_NAMING_TOKENS, {"artist", "title"}),
    "movie_naming": (MOVIE_NAMING_TOKENS, {"title"}),
    "episode_naming": (EPISODE_NAMING_TOKENS, {"series", "ss", "ee"}),
}


def _directory(spec: SettingSpec, value: str):
    if not value.startswith("/") or not Path(value).is_dir():
        raise SettingsValidationError(spec.key, "not_a_directory")


def _template(spec: SettingSpec, value: str):
    allowed, required = _TEMPLATES[spec.key]
    tokens = set(re.findall(r"\{(\w+)\}", value))
    if not tokens <= allowed or not required <= tokens:
        raise SettingsValidationError(spec.key, "bad_template")


def _channel_order(spec: SettingSpec, value: str):
    names = [part.strip() for part in value.split(",") if part.strip()]
    if (not names or len(names) != len(set(names))
            or any(n not in MUSIC_CHANNELS for n in names)):
        raise SettingsValidationError(spec.key, "bad_value")


def _bad_value_unless(accepts: Callable[[str], object]):
    def rule(spec: SettingSpec, value: str):
        if not accepts(value):
            raise SettingsValidationError(spec.key, "bad_value")
    return rule


_RULES: dict[str, Callable[[SettingSpec, str], None]] = {
    **dict.fromkeys(DIR_KEYS, _directory),
    **dict.fromkeys(_TEMPLATES, _template),
    "channel_order": _channel_order,
    "subtitle_langs": _bad_value_unless(LANGS_RE.match),
    "streaming_subscriptions": _bad_value_unless(
        lambda v: not v or re.match(r"^\d+(,\d+)*$", v)),
    **dict.fromkeys(("category_movies", "category_tv"), _bad_value_unless(
        lambda v: re.fullmatch(r"[a-z0-9_-]+", v))),
    **dict.fromkeys(("bitrate_floor", "bitrate_ceiling"), _bad_value_unless(
        lambda v: BITRATES_RE.match(v.strip()))),
}


def _validate(spec: SettingSpec, value: str):
    rule = _RULES.get(spec.key)
    if rule is not None:
        rule(spec, value)


class RuntimeConfig(_RuntimeConfig):
    spec = SETTINGS_SPEC

    def langs(self, key: str = "subtitle_langs") -> list[str]:
        return [p.strip() for p in self.get(key).split(",") if p.strip()]

    def rates(self, key: str) -> dict[str, float]:
        """A per-resolution rate setting, as {resolution: megabits}."""
        out = {}
        for part in self.get(key).split(","):
            resolution, _, rate = part.strip().partition(":")
            try:
                out[resolution] = float(rate)
            except ValueError:
                continue
        return out


store = Store(RuntimeConfig, Setting, SessionLocal, _validate)
current_runtime = store.runtime
forget_runtime = store.forget
get_for_ui = store.for_ui
update_settings = store.update
store_credentials = store.store_credentials
