# syntax=docker/dockerfile:1.7
# TwitchClip AI – ein Image für Web, Worker, Scheduler und Migrationen.

############################
# Build-Stufe: Abhängigkeiten in ein venv installieren
############################
FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
 && pip install -r /tmp/requirements.txt

# Optional: Entwicklungs-/Testabhängigkeiten (docker compose run --rm test)
ARG INSTALL_DEV=false
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r /tmp/requirements-dev.txt; fi

############################
# Laufzeit-Stufe
############################
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="TwitchClip AI" \
      org.opencontainers.image.description="Rechtekonforme Twitch-Highlight-Erkennung und TikTok-Veröffentlichung"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TZ=Europe/Berlin \
    DATA_DIR=/data \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    HF_HOME=/data/models/huggingface

# FFmpeg: Audioanalyse und Videoschnitt (inkl. libass für Untertitel).
# fonts-dejavu-core: Schrift für eingebrannte Untertitel/Überschriften (libass braucht installierte Fonts).
# tini: sauberes Signal-Handling (SIGTERM bei docker stop) und Zombie-Reaping.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

# Unprivilegierter Benutzer
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown app:app /data

COPY --from=builder /opt/venv /opt/venv

# Code gehört root und ist für den Laufzeitbenutzer nur lesbar (Härtung).
# Schreibbar ist ausschließlich /data.
WORKDIR /app
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests

USER app

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "2", "--no-server-header"]
