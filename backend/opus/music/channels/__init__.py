from opus_core import plugins

from opus.music.channels.base import Channel
from opus.music.channels.sabnzbd import SabnzbdChannel
from opus.music.channels.slskd import SlskdChannel
from opus.plugins import CHANNELS
from opus.settings_store import RuntimeConfig


def enabled_channels(config: RuntimeConfig) -> list[Channel]:
    channels: list[Channel] = [SlskdChannel(config), SabnzbdChannel(config)]
    for _, path in CHANNELS:
        kind = plugins.resolve(path)
        if kind.enabled(config):
            channels.append(kind(config))
    # search/grab order is a runtime setting; unlisted channels keep their
    # built-in priority after the listed ones
    order = [part.strip() for part in config.get("channel_order").split(",") if part.strip()]
    return sorted(
        channels,
        key=lambda c: (order.index(c.name) if c.name in order else len(order) + c.priority),
    )
