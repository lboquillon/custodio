# Copyright (c) 2026 Leonardo Boquillon
# SPDX-License-Identifier: MIT
#
# Production image: Python 3.12 with the full Presidio NER stack and a spaCy
# model baked in. Everything is self-contained; the host needs only Docker.

FROM python:3.12-slim AS base

# spaCy model to bundle. en_core_web_lg = best recall (~560MB),
# en_core_web_sm = small/fast. Override at build time:
#   docker build --build-arg SPACY_MODEL=en_core_web_sm .
ARG SPACY_MODEL=en_core_web_lg

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CUSTODIO_HOST=0.0.0.0 \
    CUSTODIO_PORT=3000 \
    CUSTODIO_ENGINE=presidio \
    CUSTODIO_SPACY_MODEL=${SPACY_MODEL}

WORKDIR /app

# Install dependencies first (better layer caching), then the package.
COPY pyproject.toml README.md ./
COPY custodio ./custodio
RUN pip install --upgrade pip \
    && pip install ".[full,redis]" \
    && python -m spacy download ${SPACY_MODEL}

# Drop privileges.
RUN useradd --create-home --uid 10001 custodio
USER custodio

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3000/custodio/health').status==200 else 1)"

ENTRYPOINT ["custodio", "serve"]
