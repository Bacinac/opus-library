# OPUS demo

The public demo is three independent static origins, one per module:

| Module | Address | Cloudflare Pages project |
|---|---|---|
| Player | https://demo-opus.boskovic.biz (`?surface=tv` for the television) | `opus-demo` |
| Library | https://demo-opus-library.boskovic.biz | `opus-library-demo` |
| Downloads | https://demo-opus-downloads.boskovic.biz | `opus-downloads-demo` |

Each is the production Svelte application with a synthetic API loaded before
the application starts. It contains fictional artists, releases, downloads,
films, series, web videos, photographs, people, places, accounts and devices.
No request can reach an OPUS backend. Changes live only in the current browser
tab and disappear on reload. The demo speaks English until the visitor picks
another language, and signs in as the fictional administrator `demo`; it never
asks for a real credential.

## Build and publish

```bash
./deploy/demo.sh            # build + gate into release/demo/{player,library,downloads}
./deploy/demo.sh --deploy   # additionally publish the three Pages projects
```

The script builds all three modules from the sibling checkouts
(`OPUS_DOWNLOADS_SOURCE` and `OPUS_PLAYER_SOURCE` override the paths) in a
`node:24` container, installing each frontend's dependencies from its lockfile.
Every module links to the other two by their public origin; override
`OPUS_DEMO_PLAYER_URL`, `OPUS_DEMO_LIBRARY_URL` and `OPUS_DEMO_DOWNLOADS_URL`
to build for other origins. The Player demo serves the signed OPUS TV and OPUS
Music APKs from `opus-player/android/dist`, as an installed Player does.

Before anything can be published, every built file goes through the clone's
`publicgate.command`; a clone without one does not build the demo at all.
Publishing needs a Cloudflare Pages token in `.cf-token`.

A single module's demo can be built on its own with `frontend/build-demo.sh`
in that module's repository. For a local look at one:

```bash
docker run --rm -p 8088:80 -v "$PWD/release/demo/library:/usr/share/nginx/html:ro" nginx:alpine
```
