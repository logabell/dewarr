# Static assets are identical for both runtime architectures; never compile them under QEMU.
FROM --platform=$BUILDPLATFORM node:24.13.0-bookworm-slim@sha256:4660b1ca8b28d6d1906fd644abe34b2ed81d15434d26d845ef0aced307cf4b6f AS web
WORKDIR /build
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/ ./
RUN npm run build

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS backend
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY services/ ./services/
COPY docs/notices/ ./docs/notices/
RUN uv sync --frozen --no-dev --no-editable
RUN .venv/bin/python -c "from app.domain.discovery_catalog import catalog; assert catalog(), 'Bundled discovery catalog is missing'"

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ARG BOOK_BUILD_VERSION=
ARG BOOK_RELEASE_REPOSITORY=logabell/dewarr
ENV BOOK_BUILD_VERSION=$BOOK_BUILD_VERSION BOOK_RELEASE_REPOSITORY=$BOOK_RELEASE_REPOSITORY
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=backend /app/.venv /app/.venv
COPY services/ ./services/
COPY alembic.ini ./
COPY docs/notices/ ./docs/notices/
COPY LICENSE ./
COPY --from=web /build/dist ./apps/web/dist/
LABEL org.opencontainers.image.title="Dewarr" \
      org.opencontainers.image.description="Audiobook discovery, reading lists and downloads" \
      org.opencontainers.image.source="https://github.com/logabell/dewarr" \
      org.opencontainers.image.licenses="MIT"
EXPOSE 8000
ENV PUID=1000 PGID=1000 TZ=Etc/UTC
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=3)"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "app.container"]
