"""What a plugin hands OPUS · Library (``opus_core.plugins`` finds and loads it).

Names arrive as data and code as ``package.module:attribute`` paths, resolved
when first used: the settings, the catalogue and the channels ask which plugins
there are while they are still being put together, and a plugin importing them
back at that moment would be a cycle."""

from dataclasses import dataclass, field

from opus_core import plugins
from opus_core.settings import SettingSpec

from opus.models import Artist, ChannelName, Release, Track


@dataclass(frozen=True)
class Plugin:
    # music catalogues trusted above Deezer, in order, as (source, path): the
    # source names the catalogue and the `<source>_id` columns it fills, and the
    # object at the path is a Catalog (opus.music.metadata.catalog)
    catalogs: tuple[tuple[str, str], ...] = ()
    # music acquisition channels, as (name, path of a Channel class)
    channels: tuple[tuple[str, str], ...] = ()
    # accounts linked through a device-code approval, as (name, path of a Link
    # (opus.api.routers.links))
    links: tuple[tuple[str, str], ...] = ()
    # routers of its own, each mounted under /api
    routes: tuple[str, ...] = ()
    settings: tuple[SettingSpec, ...] = ()
    words: dict[str, dict[str, str]] = field(default_factory=dict)


PLUGINS: tuple[Plugin, ...] = plugins.load("opus-library")

if not all(isinstance(plugin, Plugin) for plugin in PLUGINS):
    raise plugins.PluginError("a plugin handed OPUS · Library something that is not an opus.plugins.Plugin")
for _source in (source for plugin in PLUGINS for source, _ in plugin.catalogs):
    if not all(hasattr(model, f"{_source}_id") for model in (Artist, Release, Track)):
        raise plugins.PluginError(f"the {_source} catalogue has no {_source}_id column to fill")
for _name in (name for plugin in PLUGINS for name, _ in plugin.channels):
    if _name not in {channel.value for channel in ChannelName}:
        raise plugins.PluginError(f"the {_name} channel is not one a download can be recorded under")

CATALOGS: tuple[tuple[str, str], ...] = tuple(entry for plugin in PLUGINS for entry in plugin.catalogs)
CHANNELS: tuple[tuple[str, str], ...] = tuple(entry for plugin in PLUGINS for entry in plugin.channels)
LINKS: tuple[tuple[str, str], ...] = tuple(entry for plugin in PLUGINS for entry in plugin.links)
SETTINGS: tuple[SettingSpec, ...] = tuple(spec for plugin in PLUGINS for spec in plugin.settings)
