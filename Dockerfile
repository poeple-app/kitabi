# Kitabi — production image for Google Cloud Run
FROM python:3.11-slim

# Native deps for WeasyPrint (PDF rendering) + Turkish-capable fonts.
# v1.0.3+: bundle the Debian-packaged faces we can rely on; the rest of the
# note-share font picker (Crimson Pro, Playfair Display, Cormorant Garamond,
# Lora, Merriweather) is loaded at render time via WeasyPrint's URL fetcher
# from the Google Fonts CDN. A serif fallback chain in the CSS keeps text
# Turkish-readable if the CDN call fails.
# Note: fonts-lora, fonts-crimson-pro, fonts-playfair-display etc. are NOT in
# Debian Trixie — don't add them here, they'll only break the build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libgdk-pixbuf-2.0-0 \
        libcairo2 libffi-dev shared-mime-info \
        fonts-noto fonts-noto-cjk fonts-dejavu-core \
        fonts-liberation fonts-lato fonts-ebgaramond \
        fontconfig \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy source first, then install. `pip install .` reads pyproject + the
# `kitabi/` package directory at build time, so all of them must be present
# before the install step.
COPY pyproject.toml README.md ./
COPY kitabilogo.png ./
COPY kitabi/ ./kitabi/
COPY templates/ ./templates/

RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir .

# Mount point for the GCS-backed SQLite database
RUN mkdir -p /data

# Run as non-root for safety
RUN useradd -m -u 1000 kitabi && chown -R kitabi:kitabi /app /data
USER kitabi

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# Cloud Run injects $PORT; uvicorn binds to 0.0.0.0
CMD exec uvicorn kitabi.main:app --host 0.0.0.0 --port ${PORT}
