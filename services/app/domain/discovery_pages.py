"""Read-through, reader-scoped caching of public Listopia pages beyond previews."""

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert

from app.adapters.goodreads_discovery import fetch_collection_page
from app.db.models import ProviderCache
from app.db.session import session_factory
from app.domain.cache_entries import read_through

KIND = "goodreads-list-page-v1"


def cache_key(*parts):
    return hashlib.sha256(":".join([KIND, *map(str, parts)]).encode()).hexdigest()


def collection_key(user_id, collection, suffix):
    return cache_key(user_id, collection["id"], collection["updated_at"], suffix)


async def source_page(db, user_id, collection, page):
    version = collection["updated_at"]
    key = collection_key(user_id, collection, page)
    await db.rollback()

    async def load():
        result = await fetch_collection_page(collection["source_url"], page)
        value = {
            **result,
            "kind": KIND,
            "user_id": str(user_id),
            "collection_id": collection["id"],
            "version": version,
        }
        values = {
            collection_key(user_id, collection, "count"): {"count": value["count"]},
            **{
                cache_key("book", user_id, book["external_id"]): {
                    collection["id"]: {"version": version, "book": book}
                }
                for book in value["books"]
            },
        }
        now = datetime.now(UTC)
        statement = insert(ProviderCache).values(
            [
                {
                    "key": item_key,
                    "value": item,
                    "fetched_at": now,
                    "expires_at": now + timedelta(days=1),
                }
                for item_key, item in values.items()
            ]
        )
        async with session_factory()() as cache_db, cache_db.begin():
            # Retain every accessible source reference for books on several lists.
            await cache_db.execute(
                statement.on_conflict_do_update(
                    index_elements=[ProviderCache.key],
                    set_={
                        "value": ProviderCache.value.op("||")(statement.excluded.value),
                        "fetched_at": statement.excluded.fetched_at,
                        "expires_at": statement.excluded.expires_at,
                    },
                )
            )
        return value

    value, _ = await read_through(key, load, fresh_for=timedelta(days=1), allow_stale=False)
    return value


async def collection_page(db, user_id, collection, page):
    """Project 100-book source pages into the app's 40-book batches (at most two reads)."""
    start, stop = (page - 1) * 40, page * 40
    known = await db.get(ProviderCache, collection_key(user_id, collection, "count"))
    if known and known.expires_at <= datetime.now(UTC):
        known = None
    total = known.value["count"] if known else collection["count"]
    if known and start >= total:
        return [], total
    books = []
    for number in range(start // 100 + 1, (stop - 1) // 100 + 2):
        data = await source_page(db, user_id, collection, number)
        source_start = (number - 1) * 100
        source_end = source_start + len(data["books"])
        total = max(data["count"], source_end + 1) if data["has_more"] else source_end
        books.extend(data["books"][max(0, start - source_start) : stop - source_start])
        if not data["has_more"]:
            break
    return books, total


async def find_book(db, user_id, collections, external_id):
    """Resolve visible books without making cached private list selections public."""
    for collection in collections.values():
        for book in collection["books"]:
            if book["external_id"] == external_id:
                return book
    cached = await db.get(ProviderCache, cache_key("book", user_id, external_id))
    if cached:
        for collection_id, reference in cached.value.items():
            collection = collections.get(collection_id)
            if collection and collection["updated_at"] == reference["version"]:
                return reference["book"]
    return None
