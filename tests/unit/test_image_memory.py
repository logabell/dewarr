from datetime import UTC, datetime, timedelta

from app.domain import image_memory


def test_image_cache_bounds_bytes_entries_and_expiration(monkeypatch):
    monkeypatch.setattr(image_memory, "MAX_BYTES", 10)
    monkeypatch.setattr(image_memory, "MAX_ENTRIES", 2)
    cache = image_memory.ImageMemory()
    future = datetime.now(UTC) + timedelta(hours=1)
    cache.put("a", b"aaaa", "image/jpeg", future)
    cache.put("b", b"bbbb", "image/jpeg", future)
    assert cache.get("a")[0] == b"aaaa"
    cache.put("c", b"cccc", "image/jpeg", future)
    assert cache.get("b") is None and cache.bytes == 8
    cache.put("c", b"ccccccc", "image/jpeg", future)
    assert cache.get("a") is None and cache.bytes == 7
    cache.put("oversized", b"x" * 11, "image/jpeg", future)
    assert cache.get("oversized") is None and cache.bytes == 7
    cache.put("expired", b"x", "image/jpeg", datetime.now(UTC) - timedelta(seconds=1))
    assert cache.get("expired") is None and cache.bytes == 7
