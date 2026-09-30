"""The photo half of the library.

It references nothing in `opus.music` or `opus.video`, and nothing in them
references it. What the three genuinely share sits above them: `settings_store`,
`auth`, `models.base`, and the routers that describe the installation.

There is no `channels/` here and there never will be. The other halves delegate
mechanism to OPUS · Downloads because there is something to fetch; a photograph
is already on the disk, so everything this half does is judgement."""
