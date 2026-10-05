"""Small process-local artwork cache. Callers must authorize before every lookup."""

import hashlib
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from functools import lru_cache

from fastapi import Response

MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 2048


class ImageMemory:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0

    def get(self, key):
        entry = self.entries.pop(key, None)
        if not entry:
            return None
        expires, body, kind, etag = entry
        if expires <= datetime.now(UTC):
            self.bytes -= len(body)
            return None
        self.entries[key] = entry
        return body, kind, etag

    def put(self, key, body, kind, expires, etag=None):
        old = self.entries.pop(key, None)
        if old:
            self.bytes -= len(old[1])
        if len(body) > MAX_BYTES:
            return
        while self.entries and (
            self.bytes + len(body) > MAX_BYTES or len(self.entries) >= MAX_ENTRIES
        ):
            _, removed = self.entries.popitem(last=False)
            self.bytes -= len(removed[1])
        self.entries[key] = (
            min(expires, datetime.now(UTC) + timedelta(minutes=5)),
            body,
            kind,
            etag or '"' + hashlib.sha256(body).hexdigest() + '"',
        )
        self.bytes += len(body)


@lru_cache(maxsize=1)
def image_memory(engine):
    # Changing databases must not reuse bytes from the previous database.
    return ImageMemory()


def image_response(body, kind, etag, cache_control, source, if_none_match=None):
    headers = {
        "Cache-Control": cache_control,
        "ETag": etag,
        "X-Cover-Cache": source,
        "X-Content-Type-Options": "nosniff",
    }
    tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
    if etag in tags or "*" in tags:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=kind, headers=headers)
