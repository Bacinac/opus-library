# OPUS Suite

OPUS is distributed as one suite with three modules and two normal server roles:

- **Library + Downloads** on the storage/download server.
- **Player** on the playback server, beside the GPU, HDMI output and optional USB DAC.

The modules remain separate services because they have different privileges and hardware needs. They share accounts, a signed session, navigation, and each module calls another with a token issued to it alone.

## Install the storage role

```sh
tar xzf opus-suite-0.1.0.tar.gz
cd opus-suite-0.1.0
./install.sh --role=library-downloads --host-address=192.168.50.20 \
  --player-public-url=http://192.168.50.30:5283
```

The installer creates the first administrator, starts Library and Downloads, connects them, and writes `opus-player.enrollment.env` with mode `0600`.

## Install the playback role

Securely copy the enrollment file and the same suite archive to the playback server. Mount or synchronize the five media folders so their contents match the storage server's `music`, `movies`, `television`, `video` and `photos` folders, then run:

```sh
./install.sh --role=player \
  --host-address=192.168.50.30 \
  --enrollment=/secure/path/opus-player.enrollment.env \
  --media-root=/mnt/media
```

The Player reads originals from those folders read-only. An NFS or SMB mount prepared by the host is appropriate; OPUS does not ask for storage credentials or mount remote filesystems itself.

For a one-box evaluation, use `./install.sh --role=all`. The two-server layout remains the recommended production shape.

## Addresses

| Service | Default address | Purpose |
|---|---|---|
| Library UI | `http://SERVER:5280` | catalogue, imports, metadata, photos |
| Downloads UI | `http://SERVER:5282` | engines, search and jobs |
| Downloads engine UI | `http://SERVER:8099` | bundled engine interfaces |
| Player UI | `http://PLAYER:5283` | web, mobile and television playback |

The backend ports (`8095`, `8097`, `8098`) are application APIs. Keep them on the LAN; expose the UI origins through a TLS reverse proxy. A shared parent-domain cookie can be enabled with `OPUS_COOKIE_DOMAIN` when all modules use subdomains of the same domain.

## Android applications

- `opus-tv.apk` / `opus-player.apk`: TV launcher and Player application (same signed APK).
- `opus-music.apk`: mobile music and Android Auto application.

Verify `SHA256SUMS` before sideloading. APKs are also served from Player settings after installation.
