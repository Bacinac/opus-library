# Standalone installation

Requirements: a current Docker Engine with the Compose v2 plugin, `python3`,
`curl`, and approximately 4 GiB of free space before adding media.

From a clone:

```bash
git clone --recurse-submodules https://github.com/Bacinac/opus-library.git
cd opus-library
./install.sh
```

The installer is idempotent. It generates database and session secrets, creates
local media/state directories, builds the containers, waits for database
readiness and creates the first administrator. Credentials are written once to
`.install-admin` with mode `0600`. Existing secrets, the database and media are
preserved on every later run.

A finished first install sends one anonymous request to GitHub for a one-byte file of
the release, so its download count says how many installs completed; nothing
about the machine is sent or kept. `OPUS_NO_PING=1 ./install.sh` skips it.

The UI is available on port `5280` and the API on `8095`. All five library types
are initially mounted under `volumes/`; replace the corresponding
`OPUS_*_DEVICE` values in `.env` with existing absolute media paths when needed,
then run `docker compose up -d`.

OPUS Library catalogs and judges media. Acquisition is optional and is handled
by a separate OPUS Downloads installation; connect it later from **Settings**.
When adding Downloads or Player, copy this installation's `OPUS_SESSION_KEY`
to their `.env` files so all OPUS modules can read the same browser session.

Useful commands:

```bash
docker compose ps
docker compose logs -f backend frontend
docker compose up -d
docker compose down
./install.sh --no-start
```

Stopping the stack does not remove data. Do not add `-v` to `docker compose
down`: named and bind-mounted data are intentionally retained.
