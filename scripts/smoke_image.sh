#!/usr/bin/env bash
# Exercise the final runtime on its native architecture, including migrations and assets.
set -euo pipefail

docker run --rm --entrypoint python \
  -e EXPECTED_ARCH -e EXPECTED_VERSION "$IMAGE" -c '
import os, platform
from pathlib import Path
from app.domain.discovery_catalog import catalog
assert platform.machine() == {"amd64": "x86_64", "arm64": "aarch64"}[os.environ["EXPECTED_ARCH"]]
assert os.environ["BOOK_BUILD_VERSION"] == os.environ["EXPECTED_VERSION"]
assert catalog(), "Packaged catalog missing"
assert Path("/app/apps/web/dist/index.html").is_file(), "Frontend assets missing"
'

cleanup() {
  docker logs dewarr-smoke || true
  docker rm -f dewarr-smoke >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker run --detach --name dewarr-smoke --network host \
  -e BOOK_DATABASE_URL=postgresql+psycopg://book:book-test-only@127.0.0.1:5432/container_test \
  -e BOOK_PUBLIC_URL=http://127.0.0.1:8000 -e BOOK_COOKIE_SECURE=false \
  "$IMAGE"

python3 - <<'PY'
import time
import urllib.error
import urllib.request

deadline = time.monotonic() + 90
while True:
    try:
        with urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=2) as response:
            assert response.status == 200
        break
    except (urllib.error.URLError, TimeoutError):
        if time.monotonic() >= deadline:
            raise
        time.sleep(1)
with urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5) as response:
    assert b'<html' in response.read(), 'Application did not serve the bundled frontend'
PY
