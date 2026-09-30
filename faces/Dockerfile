# Intel's own runtime and not our Debian 13, deliberately. Debian trixie ships
# the Level Zero loader but not Intel's GPU driver — intel-opencl-icd and
# libze-intel-gpu1 are simply not in the archive, checked with contrib and
# non-free enabled. Everything OPUS builds is trixie; a vendor's runtime follows
# the vendor, and maintaining Intel's apt repo inside our own image for one
# container does not pay.
FROM openvino/ubuntu24_runtime:2026.4.0

USER root
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir --break-system-packages --require-hashes -r requirements.lock

COPY app.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 8099
ENTRYPOINT ["./entrypoint.sh"]
