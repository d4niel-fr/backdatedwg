# Backdate.dwg — web app + conversion engines in one image.
#
#   docker build -t backdate .
#   docker run -p 8000:8000 backdate        -> http://localhost:8000
#
# DWG output needs ODA File Converter (free download, proprietary licence).
# The build downloads it from ODA_DEB_URL, or installs a .deb you put in
# vendor/. Without it the app still runs, but only saves DXF.

# ── LibreDWG: fallback DWG reader ──────────────────────────────────────────
FROM debian:bookworm-slim AS libredwg
ARG LIBREDWG_VERSION=0.13.3
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf automake libtool texinfo pkg-config git ca-certificates python3 \
 && git clone --depth 1 --branch "${LIBREDWG_VERSION}" https://github.com/LibreDWG/libredwg /src \
 && cd /src \
 && git submodule update --init --depth 1 \
 && sh autogen.sh \
 && ./configure --disable-bindings --disable-docs --prefix=/opt/libredwg \
 && make -j"$(nproc)" \
 && make install

# ── app ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# Check https://www.opendesign.com/guestfiles/oda_file_converter for the
# current Linux (Qt6, x64) .deb and pass it with --build-arg ODA_DEB_URL=...
ARG ODA_DEB_URL="https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_25.12.deb"
ARG REQUIRE_ODA=0

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BACKDATE_DATA_DIR=/data \
    PATH="/opt/libredwg/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/libredwg/lib"

# ODA File Converter is a Qt app: it runs headless under xvfb.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      xvfb xauth curl ca-certificates \
      libglib2.0-0 libgl1 libegl1 libfontconfig1 libfreetype6 libdbus-1-3 \
      libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
      libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
      libxcb-xkb1 libxrender1 libxi6 libsm6 libice6 \
 && rm -rf /var/lib/apt/lists/*

COPY vendor/ /tmp/vendor/
RUN set -eu; \
    deb="$(ls /tmp/vendor/*.deb 2>/dev/null | head -n 1 || true)"; \
    if [ -z "$deb" ] && [ -n "$ODA_DEB_URL" ]; then \
      if curl -fsSL "$ODA_DEB_URL" -o /tmp/oda.deb; then deb=/tmp/oda.deb; \
      else echo "WARNING: couldn't download ODA File Converter from $ODA_DEB_URL"; fi; \
    fi; \
    if [ -n "$deb" ]; then \
      apt-get update && apt-get install -y --no-install-recommends "$deb" && rm -rf /var/lib/apt/lists/*; \
    fi; \
    rm -rf /tmp/vendor /tmp/oda.deb; \
    if command -v ODAFileConverter >/dev/null || ls /usr/bin/ODAFileConverter* /opt/ODAFileConverter* >/dev/null 2>&1; then \
      echo "ODA File Converter installed"; \
    elif [ "$REQUIRE_ODA" = "1" ]; then \
      echo "ERROR: ODA File Converter is required (REQUIRE_ODA=1) but isn't installed"; exit 1; \
    else \
      echo "WARNING: building without ODA File Converter: DWG output will be unavailable"; \
    fi

COPY --from=libredwg /opt/libredwg /opt/libredwg

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ app/
COPY static/ static/

RUN useradd --create-home --uid 1000 backdate && mkdir -p /data && chown backdate /data
USER backdate

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
# Jobs live in memory, so run exactly one worker process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
