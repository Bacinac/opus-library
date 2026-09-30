FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# ffmpeg: the audio judge, and ffprobe subtitle/stream inspection on the video
# side.
# alass: language-agnostic subtitle re-sync (align a mis-timed sub to a
# reference already in sync with the video).
# exiftool: when a photograph was taken, straight from upstream because Debian
# freezes at 13.25 and a photo library is exactly the thing that meets a camera
# newer than its distribution. Taken from the project's own GitHub tag —
# exiftool.org refuses a plain download.
# libvips: one decode per photograph, three encodes out of it. libheif's decode
# plugins arrive as dependencies, but the AVIF ENCODER does not and must be
# named — and its absence is not a build error, it is a runtime failure that
# writes a zero-byte file and reports success. Proven on this box: without
# libheif-plugin-aomenc, `vips copy … .avif` exits non-zero AND leaves 0 bytes
# behind. The assertion below is what stops that reaching a scan over 45,000
# photographs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl perl \
        libvips42t64 libvips-tools libheif-plugin-aomenc \
    && curl -fsSL https://github.com/kaegi/alass/releases/download/v2.0.0/alass-linux64 \
        -o /usr/local/bin/alass \
    && chmod +x /usr/local/bin/alass \
    && curl -fsSL https://github.com/exiftool/exiftool/archive/refs/tags/13.59.tar.gz \
        -o /tmp/exiftool.tar.gz \
    && tar xzf /tmp/exiftool.tar.gz -C /opt \
    && ln -s /opt/exiftool-13.59/exiftool /usr/local/bin/exiftool \
    && rm -f /tmp/exiftool.tar.gz \
    && rm -rf /var/lib/apt/lists/* \
    && vips black /tmp/probe.v 8 8 \
    && vips copy /tmp/probe.v "/tmp/probe.avif[compression=av1,encoder=aom,effort=2,Q=63]" \
    && test -s /tmp/probe.avif \
    && rm -f /tmp/probe.v /tmp/probe.avif

RUN pip install --no-cache-dir uv

COPY requirements.lock ./
RUN uv pip install --system --require-hashes -r requirements.lock
RUN --mount=type=bind,from=plugins,target=/plugins \
    for lock in /plugins/*/requirements/opus-library.lock; do \
        [ ! -f "$lock" ] || uv pip install --system --require-hashes -r "$lock"; \
    done

COPY . .

ENV PYTHONDONTWRITEBYTECODE=1

CMD ["./entrypoint.sh"]
