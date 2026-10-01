# OPUS demo

The public demo is three independent static origins, one per module:

| Module | Address | Cloudflare Pages project |
|---|---|---|
| Player | https://demo-opus.boskovic.biz (`?surface=tv` for the television) | `opus-demo` |
| Library | https://demo-opus-library.boskovic.biz | `opus-library-demo` |
| Downloads | https://demo-opus-downloads.boskovic.biz | `opus-downloads-demo` |

Each is the production Svelte application with a synthetic API loaded before
the application starts. The household inside is invented: the Kovač family,
six artists with twelve albums, eight films and a series, thirty-two
photographs from seven places, the people in them, a download queue, accounts
and devices.
No request can reach an OPUS backend. Changes live only in the current browser
tab and disappear on reload. The demo speaks English until the visitor picks
another language, and signs in as the fictional administrator `demo`; it never
asks for a real credential.

## The catalogue

Library owns the demo catalogue in `frontend/demo/`: `demo-fixtures.json` (every
row the synthetic API answers with, and the credits), `media/` (the pictures,
WebP) and `demo-credits.html` (the page the demo banner links to, built from
the fixture's credits). `frontend/demo-catalogue.sh` places all of it into a
built demo; the Player demo runs the same script from the Library checkout
beside it and shows the same household through its own screens. Nothing in
`frontend/demo/` is part of a production build.

## Credits

The films and the series are Blender open movies under Creative Commons
licences; the pictures come from Wikimedia Commons, and every file's author,
licence, source and the change made to it is listed in the fixture's `credits`
and shown at `/demo-credits.html` in the Library and Player demos.

| Work | Author | Licence |
|---|---|---|
| Big Buck Bunny (2008) | Blender Foundation, peach.blender.org | CC BY 3.0 |
| Elephants Dream (2006) | Orange Open Movie Team, Blender Foundation | CC BY 2.5 |
| Sintel (2010) | Blender Foundation, durian.blender.org | CC BY 3.0 |
| Tears of Steel (2012) | Blender Foundation, mango.blender.org | CC BY 3.0 |
| Cosmos Laundromat (2015) | Blender Foundation, Andy Goralczyk | CC BY 3.0 (still), CC BY 4.0 (poster) |
| Agent 327: Operation Barbershop (2017) | Blender Animation Studio | CC BY 3.0 |
| Spring (2019) | Blender Foundation, Andy Goralczyk | CC BY 4.0 |
| Sprite Fright (2021) | Blender Studio | CC BY 4.0 |
| Caminandes (2013–2016) | Pablo Vazquez, Beorn Leonard and Francesco Siddi, Blender Foundation | CC BY 3.0 |

Posters are the films' own where one exists; for Elephants Dream, Spring,
Agent 327 and Caminandes a frame was cropped and the title set over it.

The photographs are CC0 works from Wikimedia Commons by Jebulon, Wilfredor,
DimiTalen, Benoît Prieur, Bernard Gagnon, Peter Cooper Jr., Romzig, W.carter,
Gestumblindi, Vassil, Velatrix, Dktue and Roc0ast3r; their dates and places in
the demo are invented.

The portraits, album covers, artist and web-video pictures were generated for
the demo with Realistic Vision 5.1 (CreativeML OpenRAIL-M) and show no real
person. The artists, records and household are fictional.

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
in that module's repository; the Player's takes the catalogue from the Library
checkout beside it (`OPUS_LIBRARY_SOURCE` overrides the path). For a local look
at one:

```bash
docker run --rm -p 8088:80 -v "$PWD/release/demo/library:/usr/share/nginx/html:ro" nginx:alpine
```
