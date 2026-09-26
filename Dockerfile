FROM mcr.microsoft.com/playwright:v1.61.0-noble@sha256:57b65fdc9ceabe0ef613124c7bbe2babcf9362c4d85e382fe3b03604e84b428a AS frontend-build

ARG TARGETPLATFORM
RUN test "${TARGETPLATFORM}" = "linux/amd64"

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,id=jav-pilot-npm,target=/root/.npm,sharing=locked \
    test "$(npm --version)" = "11.13.0" && \
    npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69 AS ffmpeg-build

ARG TARGETPLATFORM
RUN test "${TARGETPLATFORM}" = "linux/amd64"

# The source digest was independently verified with FFmpeg's official release
# signing key (FCF986EA15E6E293A5644F10B4322F04D67658D8) before it was pinned.
RUN --mount=type=cache,id=jav-pilot-ffmpeg-apt,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,id=jav-pilot-ffmpeg-apt-lists,target=/var/lib/apt/lists,sharing=locked \
    set -eu; \
    export DEBIAN_FRONTEND=noninteractive; \
    FFMPEG_VERSION=9.0.1; \
    FFMPEG_SHA256=cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635; \
    rm -f /etc/apt/apt.conf.d/docker-clean; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential ca-certificates curl nasm xz-utils zlib1g-dev; \
    curl --fail --location --proto '=https' --tlsv1.2 \
        --retry 5 --retry-all-errors --connect-timeout 15 \
        "https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz" \
        --output /tmp/ffmpeg.tar.xz; \
    echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum --check --strict; \
    mkdir /tmp/ffmpeg-source; \
    tar --extract --xz --file /tmp/ffmpeg.tar.xz \
        --strip-components=1 --directory /tmp/ffmpeg-source; \
    cd /tmp/ffmpeg-source; \
    ./configure \
        --prefix=/opt/ffmpeg \
        --cpu=generic \
        --disable-autodetect \
        --disable-avdevice \
        --disable-debug \
        --disable-demuxers \
        --disable-doc \
        --disable-encoders \
        --disable-ffplay \
        --disable-hwaccels \
        --disable-indevs \
        --disable-muxers \
        --disable-network \
        --disable-outdevs \
        --disable-protocols \
        --disable-shared \
        --enable-demuxer=asf,avi,flv,matroska,mov,mpegps,mpegvideo,mpegts,rm \
        --enable-muxer=mov,mp4 \
        --enable-protocol=file \
        --enable-pthreads \
        --enable-small \
        --enable-static \
        --enable-zlib \
        --extra-cflags='-O2 -fstack-protector-strong -fstack-clash-protection -fcf-protection -D_FORTIFY_SOURCE=3' \
        --extra-ldflags='-Wl,-z,relro,-z,now'; \
    make -j2; \
    make install; \
    strip /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe; \
    /opt/ffmpeg/bin/ffmpeg -hide_banner -protocols 2>&1 \
        | grep -Eq '^  file$'; \
    ! /opt/ffmpeg/bin/ffmpeg -hide_banner -protocols 2>&1 \
        | grep -Eq '^  https?$'

FROM mcr.microsoft.com/playwright/python:v1.61.0-noble@sha256:a9731514f24121d1dcd25d58d0a38146646d290a5998fd80d3e533e7b5e21c69

ARG TARGETPLATFORM
RUN test "${TARGETPLATFORM}" = "linux/amd64"

WORKDIR /app
COPY --from=ffmpeg-build /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg-build /opt/ffmpeg/bin/ffprobe /usr/local/bin/ffprobe
RUN set -eu; \
    ! ldd /usr/local/bin/ffmpeg /usr/local/bin/ffprobe | grep -F "not found"; \
    ffmpeg -version | grep -F "ffmpeg version 9.0.1" >/dev/null; \
    ffprobe -version | grep -F "ffprobe version 9.0.1" >/dev/null
# The dependency lock and its verifier are only needed while installing, so
# they are mounted for this step instead of being copied into the image.
RUN --mount=type=cache,id=jav-pilot-pip,target=/root/.cache/pip,sharing=locked \
    --mount=type=bind,source=pyproject.toml,target=/tmp/build/pyproject.toml \
    --mount=type=bind,source=requirements.lock,target=/tmp/build/requirements.lock \
    --mount=type=bind,source=tools/verify_python_lock.py,target=/tmp/build/verify_python_lock.py \
    cd /tmp/build && \
    python verify_python_lock.py && \
    python -m pip install --disable-pip-version-check \
        --require-hashes --only-binary=:all: --requirement requirements.lock && \
    python verify_python_lock.py --verify-installed-closure

COPY LICENSE ./
ARG VCS_REF=unknown

LABEL org.opencontainers.image.title="JAV Pilot" \
      org.opencontainers.image.version="0.0.1" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/drdon1234/JAV-Pilot" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV PYTHONUNBUFFERED=1 \
    JAV_PILOT_JAVDB_FETCHER=auto \
    JAV_PILOT_BROWSER_WAIT_MS=3000 \
    JAV_PILOT_STATIC_DIR=/app/frontend/dist \
    JAV_PILOT_REVISION=${VCS_REF}

COPY jav_pilot ./jav_pilot
COPY --from=frontend-build /build/frontend/dist ./frontend/dist
RUN install -d -o pwuser -g pwuser -m 0750 \
    /app/data /app/browser-profile /downloads/jav /downloads/jav-web /media/JAV

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8766/readyz', timeout=3).read()"]
CMD ["python", "-m", "jav_pilot.cli", "serve", "--host", "0.0.0.0", "--port", "8766"]
