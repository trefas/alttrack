# alttrack — image for `alttrack serve` (web dashboard + background refresh).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ALTTRACK_DATA_DIR=/data \
    ALTTRACK_CONFIG=/config/config.toml \
    ALTTRACK_HOST=0.0.0.0 \
    ALTTRACK_PORT=8300

# Non-root user; persistent dirs for the SQLite database and config.toml.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data /config \
    && chown -R app:app /data /config

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

USER app

EXPOSE 8300

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ALTTRACK_PORT', '8300'), timeout=5)"

CMD ["alttrack", "serve"]
